"""File validation and safe application for generated project scaffolds."""
from __future__ import annotations
import difflib
import json
import os
import re
from pathlib import Path, PurePosixPath

MAX_FILES = 35
MAX_FILE_BYTES = 100_000
MAX_TOTAL_BYTES = 600_000
DENIED_NAMES = {'.env', '.env.local', '.env.production', 'id_rsa', 'id_ed25519'}
DENIED_SUFFIXES = {'.pem', '.key', '.p12', '.pfx'}

class ValidationError(ValueError):
    pass

def clean_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or '\\' in raw or '\x00' in raw:
        raise ValidationError('Use a nonempty relative path with forward slashes')
    if raw.startswith('/') or re.match(r'^[A-Za-z]:', raw):
        raise ValidationError('Absolute paths are forbidden')
    parts = raw.split('/')
    if any(p in ('', '.', '..') for p in parts):
        raise ValidationError('Empty, dot and parent path components are forbidden')
    if any(p.endswith((' ', '.')) or ':' in p for p in parts):
        raise ValidationError('Invalid Windows path component')
    if any(p.upper().split('.')[0] in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))} for p in parts):
        raise ValidationError('Windows reserved filename')
    name = parts[-1].lower()
    if name in DENIED_NAMES or PurePosixPath(name).suffix.lower() in DENIED_SUFFIXES:
        raise ValidationError('Secret/credential file generation forbidden')
    return '/'.join(parts)

def parse_files(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f'Model did not return valid JSON: {exc}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('files'), list):
        raise ValidationError('Expected JSON object with files array')
    files = data['files']
    if not files or len(files) > MAX_FILES:
        raise ValidationError(f'File count must be 1..{MAX_FILES}')
    seen, total, result = set(), 0, []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get('content'), str):
            raise ValidationError('Each file requires path and string content')
        path = clean_path(item.get('path'))
        if path.lower() in seen:
            raise ValidationError('Duplicate path')
        seen.add(path.lower())
        content = item['content']
        size = len(content.encode('utf-8'))
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValidationError('Generated content exceeds safety limits')
        if path.endswith('.json'):
            try: json.loads(content)
            except json.JSONDecodeError as exc: raise ValidationError(f'Invalid JSON in {path}: {exc}') from exc
        if path.endswith('.py'):
            try: compile(content, path, 'exec')
            except SyntaxError as exc: raise ValidationError(f'Invalid Python in {path}: {exc}') from exc
        result.append({'path': path, 'content': content})
    return result

def _safe_target(root: Path, relative: str) -> Path:
    root = root.resolve(strict=False)
    target = root.joinpath(*relative.split('/'))
    if not target.resolve(strict=False).is_relative_to(root):
        raise ValidationError('Path escapes project root (including symlink)')
    return target

def preview(root: Path, files: list[dict]) -> list[dict]:
    entries = []
    for f in files:
        target = _safe_target(root, f['path'])
        if target.is_symlink():
            raise ValidationError('Symlink target forbidden')
        existing = target.exists()
        if existing and not target.is_file():
            raise ValidationError(f'Not a regular file: {f["path"]}')
        old = target.read_text(encoding='utf-8') if existing else ''
        diff = ''.join(difflib.unified_diff(old.splitlines(True), f['content'].splitlines(True), fromfile='existing/'+f['path'], tofile='proposed/'+f['path']))
        entries.append({'path': f['path'], 'status': 'overwrite' if existing else 'create', 'diff': diff[:12000]})
    return entries

def apply(root: Path, files: list[dict], allow_overwrite: bool = False) -> list[str]:
    """Write only validated files. Refuse all overwrites unless explicitly approved.

    Uses exclusive creation for new files and atomic replacement for approved overwrites.
    Do not expose this local-only service to untrusted users.
    """
    preview_items = preview(root, files)
    if any(item['status'] == 'overwrite' for item in preview_items) and not allow_overwrite:
        raise ValidationError('Existing files found; explicit overwrite approval required')
    root.mkdir(parents=True, exist_ok=True)
    written = []
    for f in files:
        target = _safe_target(root, f['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        if allow_overwrite and target.exists():
            import tempfile
            fd, temp = tempfile.mkstemp(prefix='.euri-', dir=target.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream: stream.write(f['content'])
                os.replace(temp, target)
            finally:
                if os.path.exists(temp): os.unlink(temp)
        else:
            with target.open('x', encoding='utf-8', newline='\n') as stream: stream.write(f['content'])
        written.append(f['path'])
    return written
