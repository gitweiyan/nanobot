# AGENTS.md

## Cursor Cloud specific instructions

### Overview

nanobot is an ultra-lightweight personal AI assistant framework (Python). It provides a CLI agent, a gateway server for chat channels (Telegram, Discord, WhatsApp, etc.), and connects to various LLM providers. There is also an optional WhatsApp bridge (Node.js/TypeScript) in `bridge/`.

### Development commands

See `CONTRIBUTING.md` for the canonical dev setup. Key commands:

- **Install dev deps:** `pip install -e ".[dev]"`
- **Lint:** `ruff check nanobot/`
- **Format:** `ruff format nanobot/`
- **Tests:** `python3 -m pytest tests/ -v`
- **Run CLI agent:** `nanobot agent -m "Hello!"`
- **Run gateway:** `nanobot gateway`
- **Show status:** `nanobot status`
- **Initialize config:** `nanobot onboard`

### Non-obvious notes

- The `nanobot` CLI script installs to `~/.local/bin/`. Ensure `PATH` includes `$HOME/.local/bin`.
- An LLM provider API key (e.g. `OPENROUTER_API_KEY`) must be configured in `~/.nanobot/config.json` under `providers` before `nanobot agent` or `nanobot gateway` will work. Without it, they exit with "No API key configured."
- Run `nanobot onboard` once to initialize `~/.nanobot/config.json` and `~/.nanobot/workspace/`.
- All persistence is file-based (JSONL, JSON, markdown) — no external databases are needed.
- The test suite uses mocking and does **not** require any running services or API keys. 6 pre-existing test failures exist on `main` (config paths, matrix media, tool validation).
- `ruff` reports ~67 pre-existing import-sorting warnings; these are in the upstream codebase.
- The `[dev]` extras include `matrix-nio[e2e]` which requires system package `libolm-dev` for encryption support. This is pre-installed on the Cloud VM.
- Python ≥ 3.11 is required. The VM has Python 3.12.
