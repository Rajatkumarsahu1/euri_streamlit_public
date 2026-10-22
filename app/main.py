"""Local-only Euri Project Creator API. Start: python -m uvicorn app.main:app --host 127.0.0.1 --port 8765"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .core import ValidationError, apply, parse_files, preview

load_dotenv(override=True)
logger = logging.getLogger(__name__)
app = FastAPI(title="Euri Project Creator", version="0.7.0")
PENDING: dict[str, tuple[Path, list[dict]]] = {}
LOCK = threading.Lock()
DEFAULT_ROOT = r"H:\Projects\my-project"
PROMPT_FILE = Path(__file__).with_name("master_prompt.txt")
EURI_ENDPOINT = os.getenv("EURI_CHAT_URL", "https://api.euron.one/api/v1/euri/chat/completions")
EURI_MODEL = os.getenv("EURI_MODEL", "gpt-6-luna")


class GenerateRequest(BaseModel):
    project_folder: str = DEFAULT_ROOT
    project_description: str = "Python project configured for Claude Code CLI"
    stack: str = "python"


class ApplyRequest(BaseModel):
    preview_id: str
    approve: bool = False
    overwrite_existing: bool = False


def get_api_key(authorization: str | None) -> str:
    """Use explicitly supplied Postman Bearer token, otherwise .env key."""
    if authorization is not None:
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            raise HTTPException(401, "Invalid Authorization header; expected Bearer token")
        if token.strip() == "{{EURI_API_KEY}}":
            raise HTTPException(401, "Postman EURI_API_KEY variable was not resolved")
        return token.strip()
    key = os.getenv("EURI_API_KEY", "").strip()
    if not key:
        raise HTTPException(401, "EURI_API_KEY missing: set .env or Postman Bearer token")
    return key


# Small initial groups; any group that exceeds the output limit is split again.
BATCHES = [
    ("foundation", ["CLAUDE.md", "README.md"]),
    ("project-files", [".gitignore", ".env.example", "requirements-dev.txt"]),
    ("tooling", ["pytest.ini", "ruff.toml"]),
    ("skills-coding", [".claude/skills/coding/SKILL.md", ".claude/skills/testing/SKILL.md"]),
    ("skills-maintenance", [".claude/skills/debugging/SKILL.md", ".claude/skills/documentation/SKILL.md"]),
    ("settings", [".claude/settings.json"]),
    ("hooks", [".claude/hooks/guard.py", ".claude/hooks/format.py"]),
    ("agents-review", [".claude/agents/code-reviewer.md", ".claude/agents/debugger.md"]),
    ("agents-test", [".claude/agents/test-writer.md"]),
    ("commands", [".claude/commands/review.md", ".claude/commands/test.md", ".claude/commands/explain.md"]),
    ("documentation", ["docs/architecture.md", "docs/conventions.md"]),
]


def project_manifest() -> str:
    """A short, stable plan shared with each independent API call."""
    lines = ["PROJECT MANIFEST (all files in this project; do not claim they are absent):"]
    for _, paths in BATCHES:
        lines.extend(f"- {path}" for path in paths)
    lines.append("The settings must register guard.py as a PreToolUse hook and format.py as a PostToolUse hook.")
    lines.append("Python development tooling: pytest and ruff, declared in requirements-dev.txt.")
    return "\n".join(lines)


def normalize_project(files: list[dict]) -> list[dict]:
    """Finalize the complete scaffold deterministically; no extra model tokens."""
    by_path = {item["path"]: dict(item) for item in files}
    expected = {path for _, paths in BATCHES for path in paths}
    if set(by_path) != expected or len(files) != len(expected):
        raise ValidationError("Project manifest mismatch or duplicate files")

    # Preserve useful generated project-specific guidance, but replace misleading
    # cross-file descriptions with an authoritative, computed inventory.
    inventory = (
        "## Included Claude Code configuration\n"
        "- Skills: `coding`, `testing`, `debugging`, `documentation` in `.claude/skills/`.\n"
        "- Agents: `code-reviewer`, `debugger`, `test-writer` in `.claude/agents/`.\n"
        "- Commands: `/review`, `/test`, `/explain` in `.claude/commands/`.\n"
        "- Hooks: `.claude/hooks/guard.py` (PreToolUse) and "
        "`.claude/hooks/format.py` (PostToolUse), registered in `.claude/settings.json`.\n"
        "- Development tools: `pytest` and `ruff` from `requirements-dev.txt`.\n"
    )
    import re
    for path in ("CLAUDE.md", "README.md"):
        content = by_path[path]["content"]
        lines = []
        for line in content.splitlines():
            low = line.lower()
            # Delete future-tense and absence claims contradicted by the manifest.
            mentions = any(word in low for word in ("skill", "agent", "hook", "command", "setting"))
            absence = any(phrase in low for phrase in (
                "no additional", "no other", "not configured", "none configured",
                "if and when those files are added", "when they are added",
                "add task-specific", "add skills", "no skills", "no hooks",
            ))
            if mentions and absence:
                continue
            lines.append(line)
        # Make README authoritative on install/test commands, rather than leaving
        # potentially contradictory model-generated test instructions.
        content = "\n".join(lines).rstrip()
        if path == "README.md":
            content = (
                "# Python project\n\n"
                "Minimal Python scaffold configured for Claude Code. No application features are implemented.\n\n"
                "## Setup (Windows CMD)\n"
                "```cmd\npy -m venv .venv\n.venv\\Scripts\\activate.bat\n"
                "python -m pip install -r requirements-dev.txt\n```\n\n"
                "## Checks (Windows CMD)\n"
                "```cmd\npython -m pytest\npython -m ruff check .\n```\n\n"
                "See `CLAUDE.md` for project-wide guidance.\n"
            )
        else:
            # Remove model-provided test command bullets that conflict with pytest.
            content = re.sub(r"(?m)^.*(?:py -m unittest|python -m unittest).*$\n?", "", content)
            content = content.rstrip() + "\n\n## Validation (Windows CMD)\n"
            content += "- Run `python -m pytest` and `python -m ruff check .` after installing `requirements-dev.txt`.\n"
        by_path[path]["content"] = content.rstrip() + "\n\n" + inventory

    by_path["requirements-dev.txt"]["content"] = "pytest>=8,<10\nruff>=0.9,<1\n"
    by_path["pytest.ini"]["content"] = "[pytest]\ntestpaths = tests\npython_files = test_*.py\n"

    # Audited hook implementations replace untrusted generated executable code.
    by_path[".claude/hooks/guard.py"]["content"] = GUARD_HOOK
    by_path[".claude/hooks/format.py"]["content"] = FORMAT_HOOK
    settings = {
        "permissions": {"deny": ["Read(./.env)", "Edit(./.env)", "Write(./.env)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [
                {"type": "command", "command": "python .claude/hooks/guard.py"}]}],
            "PostToolUse": [{"matcher": "Edit|Write", "hooks": [
                {"type": "command", "command": "python .claude/hooks/format.py"}]}],
        },
    }
    by_path[".claude/settings.json"]["content"] = json.dumps(settings, indent=2) + "\n"
    finalized = [by_path[path] for _, paths in BATCHES for path in paths]
    validate_consistency(finalized)
    return finalized


GUARD_HOOK = '"""Reject edits to common secret files; Claude Code PreToolUse hook."""\nimport json\nimport sys\nfrom pathlib import PureWindowsPath\n\n\ndef main():\n    try:\n        event = json.load(sys.stdin)\n        tool_input = event.get("tool_input", {})\n        if not isinstance(tool_input, dict):\n            return 2\n        raw = tool_input.get("file_path", tool_input.get("notebook_path", ""))\n        if not isinstance(raw, str):\n            return 2\n        name = PureWindowsPath(raw).name.casefold()\n        blocked = (\n            (name.startswith(".env") and name != ".env.example")\n            or name in {"credentials.json", "secrets.json", "id_rsa", "service-account.json"}\n            or name.endswith((".pem", ".key", ".p12", ".pfx"))\n        )\n        if blocked:\n            print("Blocked: sensitive file edit.", file=sys.stderr)\n            return 2\n        return 0\n    except (ValueError, TypeError, AttributeError):\n        print("Blocked: invalid hook input.", file=sys.stderr)\n        return 2\n\n\nif __name__ == "__main__":\n    sys.exit(main())\n'

FORMAT_HOOK = '"""Format a changed Python file only if Ruff is installed; PostToolUse."""\nimport importlib.util\nimport json\nfrom pathlib import Path\nimport subprocess\nimport sys\n\n\ndef main():\n    try:\n        event = json.load(sys.stdin)\n        raw = event.get("tool_input", {}).get("file_path", "")\n        if not isinstance(raw, str) or not raw.endswith(".py"):\n            return 0\n        root = Path(event.get("cwd") or Path.cwd()).resolve()\n        target = (root / raw).resolve()\n        if not target.is_relative_to(root) or not target.is_file():\n            return 0\n        if importlib.util.find_spec("ruff") is None:\n            return 0\n        result = subprocess.run(\n            [sys.executable, "-m", "ruff", "format", "--quiet", str(target)],\n            check=False, timeout=30,\n        )\n        return result.returncode\n    except (ValueError, TypeError, AttributeError, OSError, subprocess.TimeoutExpired):\n        return 0\n\n\nif __name__ == "__main__":\n    sys.exit(main())\n'


def validate_consistency(files: list[dict]) -> None:
    """Reject previews with missing paths, broken settings or broken hook code."""
    by_path = {item["path"]: item["content"] for item in files}
    expected = {path for _, paths in BATCHES for path in paths}
    if set(by_path) != expected or len(files) != len(expected):
        raise ValidationError("Generated file inventory is incomplete")
    settings = json.loads(by_path[".claude/settings.json"])
    for event, filename in (("PreToolUse", "guard.py"), ("PostToolUse", "format.py")):
        groups = settings.get("hooks", {}).get(event, [])
        if not any(filename in hook.get("command", "") for group in groups
                   for hook in group.get("hooks", [])):
            raise ValidationError(f"{filename} is not registered for {event}")
    for filename in ("guard.py", "format.py"):
        path = ".claude/hooks/" + filename
        compile(by_path[path], path, "exec")
    for doc in ("CLAUDE.md", "README.md"):
        content = by_path[doc]
        for term in (".claude/skills/", ".claude/agents/", ".claude/commands/", ".claude/settings.json"):
            if term not in content:
                raise ValidationError(f"{doc} is missing {term}")
    if "pytest" not in by_path["requirements-dev.txt"] or "ruff" not in by_path["requirements-dev.txt"]:
        raise ValidationError("Missing declared development dependencies")
    if "python -m pytest" not in by_path["README.md"]:
        raise ValidationError("README must document pytest command")

def build_batch_prompt(description: str, paths: list[str]) -> str:
    return (
        "Configure a NEW Python project for Claude Code CLI. "
        f"Project description: {description}\n"
        + project_manifest() + "\n"
        "Generate ONLY these files, exactly once each:\n"
        + "\n".join(paths)
        + "\nReturn ONLY valid JSON: {\"files\":[{\"path\":\"...\",\"content\":\"...\"}]}. "
        "Each path and content must be a string. No Markdown fences or commentary. "
        "Keep each file concise, functional, and token-efficient. CLAUDE.md under 100 lines. "
        "Do not build application features or include secrets or a real .env file. "
        "Use Windows CMD-compatible instructions. Relative forward-slash paths only. "
        "For settings and hooks, use valid Claude Code hook configuration and Windows-compatible commands. "
        "Avoid inventing project requirements or repeating guidance across files. "
        "IMPORTANT: Use each requested filename EXACTLY, with no extra punctuation, "
        "trailing periods, trailing spaces, or colons. Do not rename files."
    )


def generate_batch(name: str, paths: list[str], description: str,
                   system_prompt: str, api_key: str, retry: int = 0) -> list[dict]:
    """Split on output-limit/invalid-JSON errors; never save a partial batch."""
    logger.info("Generating %s: %d file(s)", name, len(paths))
    try:
        prompt = build_batch_prompt(description, paths)
        if retry:
            prompt += (
                "\nIMPORTANT PATH CORRECTION: Previous output failed path validation. "
                "Copy each required path EXACTLY as shown, with no spaces, punctuation, "
                "quotes, colons, or trailing periods added to the filename. "
                "Required paths: " + json.dumps(paths) + ". "
                "Return only the JSON files object."
            )
        raw = euri_request(system_prompt, prompt, api_key)
        # Some models wrap otherwise valid JSON in Markdown fences.
        stripped = raw.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                stripped = "\n".join(lines[1:-1])
        files = parse_files(stripped)
        expected = {path.casefold() for path in paths}
        actual = {item["path"].casefold() for item in files}
        if expected != actual:
            raise ValidationError(
                f"Incorrect file set: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
            )
        return files
    except (HTTPException, ValidationError) as exc:
        # Don't retry authentication failures or arbitrary upstream failures.
        is_length = isinstance(exc, HTTPException) and "output token limit" in str(exc.detail)
        is_bad_model_json = isinstance(exc, HTTPException) and "invalid JSON response body" in str(exc.detail)
        is_parse = isinstance(exc, ValidationError) and (
            "valid JSON" in str(exc) or "Expected JSON object" in str(exc)
        )
        is_path = isinstance(exc, ValidationError) and any(
            phrase in str(exc) for phrase in (
                "Invalid Windows path component",
                "Use a nonempty relative path",
                "Absolute paths are forbidden",
                "Empty, dot and parent path components are forbidden",
                "Windows reserved filename",
                "Incorrect file set",
            )
        )
        if is_path and retry == 0:
            logger.warning("Batch %s returned invalid paths; retrying with exact filenames", name)
            return generate_batch(name, paths, description, system_prompt, api_key, retry=1)
        if not (is_length or is_parse or is_path or is_bad_model_json):
            if isinstance(exc, ValidationError):
                raise HTTPException(502, f"Batch {name} failed validation: {exc}") from exc
            raise
        if len(paths) == 1:
            reason = "invalid file path" if is_path else "output too long or invalid JSON"
            raise HTTPException(
                502, f"Batch {name} failed for {paths[0]} after retry ({reason}). "
                     "The generated file was rejected; no files were saved."
            ) from exc
        midpoint = len(paths) // 2
        logger.warning("Splitting batch %s (%d files) after incomplete response", name, len(paths))
        return (
            generate_batch(name + "-a", paths[:midpoint], description, system_prompt, api_key)
            + generate_batch(name + "-b", paths[midpoint:], description, system_prompt, api_key)
        )


def euri_request(system_prompt: str, user_prompt: str, api_key: str) -> str:
    """Send full project-generation instructions (not the earlier Hello test)."""
    payload = {
        "model": EURI_MODEL,
        "max_tokens": int(os.getenv("EURI_MAX_TOKENS", "4096")),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    request = urllib.request.Request(
        EURI_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "PostmanRuntime/7.43.0",
        },
        method="POST",
    )
    # Retry only transient network failures. Never retry bad credentials or 4xx errors.
    timeout_seconds = max(10, int(os.getenv("EURI_TIMEOUT_SECONDS", "120")))
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read()
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("Euri response was not valid JSON (body_bytes=%d)", len(body))
                raise HTTPException(
                    502, "Euri returned an invalid JSON response body; "
                         "the response was rejected. No files were saved."
                ) from exc
            break
        except urllib.error.HTTPError as exc:
            logger.warning("Euri HTTP status=%s endpoint=%s", exc.code, EURI_ENDPOINT)
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                logger.warning("Retrying transient Euri HTTP %s (attempt %d/%d)",
                               exc.code, attempt + 1, max_attempts)
                time.sleep(2)
                continue
            raise HTTPException(
                502, f"Euri returned HTTP {exc.code}; check model access, key and request limits"
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            logger.warning("Euri timed out after %ss (attempt %d/%d)",
                           timeout_seconds, attempt, max_attempts)
            if attempt < max_attempts:
                time.sleep(2)
                continue
            raise HTTPException(
                504, f"Euri timed out after {max_attempts} attempts "
                     f"({timeout_seconds}s each). Check CMD logs and retry later."
            ) from exc
        except urllib.error.URLError as exc:
            logger.warning("Euri connection failure (attempt %d/%d): %s",
                           attempt, max_attempts, type(exc.reason).__name__)
            if attempt < max_attempts:
                time.sleep(2)
                continue
            raise HTTPException(
                502, "Could not connect to Euri after two attempts"
            ) from exc
    # Log only the response shape and completion status, never generated text or secrets.
    if not isinstance(data, dict):
        raise HTTPException(502, "Euri returned a non-object JSON response")
    choices = data.get("choices")
    logger.warning(
        "Euri response metadata: keys=%s choices_type=%s choices_count=%s",
        sorted(data.keys()), type(choices).__name__,
        len(choices) if isinstance(choices, list) else "n/a",
    )
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise HTTPException(502, "Euri returned no completion choices; check CMD response metadata")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    message = choice.get("message")
    logger.warning(
        "Euri completion metadata: finish_reason=%s message_type=%s content_type=%s usage=%s",
        finish_reason, type(message).__name__,
        type(message.get("content")).__name__ if isinstance(message, dict) else "n/a",
        data.get("usage") if isinstance(data.get("usage"), dict) else "n/a",
    )
    if finish_reason == "length":
        raise HTTPException(
            502,
            "Euri generation reached the output token limit. Generate fewer files per request.",
        )
    if not isinstance(message, dict):
        raise HTTPException(502, "Euri returned a choice without a message")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        )
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(
            502,
            "Euri returned empty or non-text content; inspect CMD completion metadata",
        )
    return content



@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": EURI_MODEL,
        "api_key_configured": bool(os.getenv("EURI_API_KEY", "").strip()),
        "postman_bearer_supported": True,
    }


@app.post("/generate")
def generate(req: GenerateRequest, authorization: str | None = Header(default=None)):
    api_key = get_api_key(authorization)
    if req.stack.lower() != "python":
        raise HTTPException(400, "This starter supports Python projects only")
    root = Path(req.project_folder).expanduser()
    if not root.is_absolute():
        raise HTTPException(400, "project_folder must be an absolute path")
    if not PROMPT_FILE.is_file():
        raise HTTPException(500, "app/master_prompt.txt is missing")

    system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
    all_files: list[dict] = []
    for batch_name, paths in BATCHES:
        all_files.extend(generate_batch(
            batch_name, paths, req.project_description, system_prompt, api_key
        ))

    # Revalidate the complete set before exposing any preview or allowing writes.
    try:
        files = normalize_project(parse_files(json.dumps({"files": all_files}, ensure_ascii=False)))
        changes = preview(root, files)
    except (ValidationError, UnicodeError, OSError, SyntaxError, ValueError) as exc:
        raise HTTPException(422, f"Generated files failed validation: {exc}") from exc

    preview_id = secrets.token_urlsafe(24)
    with LOCK:
        PENDING[preview_id] = (root, files)
    return {
        "status": "preview_ready",
        "preview_id": preview_id,
        "project_folder": str(root),
        "file_count": len(files),
        "preview": changes,
    }


@app.post("/apply")
def write_files(req: ApplyRequest):
    if not req.approve:
        raise HTTPException(400, "Set approve=true only after reviewing the preview")
    with LOCK:
        item = PENDING.get(req.preview_id)
    if item is None:
        raise HTTPException(404, "Unknown preview ID; generate again (IDs reset on restart)")
    root, files = item
    try:
        paths = apply(root, files, allow_overwrite=req.overwrite_existing)
    except (ValidationError, OSError, UnicodeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    with LOCK:
        PENDING.pop(req.preview_id, None)
    return {"status": "saved", "project_folder": str(root), "files_written": paths}
