# Euri Project Creator — Streamlit edition

Public bring-your-own-Euri-key web interface for generating the existing 22-file **Python Claude Code configuration scaffold**. This reuses the original `app/main.py`, `app/core.py` and `app/master_prompt.txt`; it does not build application features.

## Run locally (Windows CMD)

```cmd
cd /d "H:\super30\Project 1\euri-streamlit-public"
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

Use the actual folder where you extracted this ZIP if different.

## Deploy on Streamlit Community Cloud

1. Push these files to a GitHub repository. **Do not commit real API keys or `.env` files.**
2. In Streamlit Community Cloud, create an app from that repository.
3. Set the entrypoint to `streamlit_app.py`.
4. No app-wide Euri API key is needed. Each user enters their own key at runtime.

## Security and limitations

- User keys are handled by Streamlit server-side code and sent to Euri via HTTPS. A password field masks the input but does **not** provide end-to-end encryption from the app operator. The hosting platform may retain runtime/session data.
- The app intentionally does not write uploaded documents, keys or generated files to disk. Keys remain in Streamlit widget session memory until cleared or the session ends. The **Clear API key and results** button clears the key and generated outputs.
- Document contents and project descriptions are sent to Euri. Users must have permission to share them.
- Each upload is limited to 2 MB and extracted text to 20,000 characters. A maximum of 3 documents is allowed. Scanned PDFs without extractable text are not supported.
- Generation can take several minutes and may incur charges on the user's Euri account. No guarantee of zero cost or of uninterrupted availability.
- The public app creates an in-memory ZIP and never writes to a visitor-specified server folder.
- Generated configuration is validated but must be reviewed. Hooks are not executed during generation.
- The original `app/main.py` still includes local-only FastAPI routes for local development; do **not** expose them as a public service. Only deploy `streamlit_app.py`.
- The scaffold is currently Python-specific, including README and pytest/Ruff normalization. Do not advertise other stacks as supported.
- Before production use, review rate limits, privacy disclosures, retention, dependency updates, and the Claude Code hook configuration against current documentation.
