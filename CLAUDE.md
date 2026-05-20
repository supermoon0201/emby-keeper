# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Embykeeper is a Python automation tool for Emby/Subsonic media server account keep-alive and Telegram bot interactions (daily check-ins, group monitoring, auto-registration, auto-chat). Chinese-language project targeting the Chinese Emby community.

## Commands

```bash
# Install
pip install -e .

# Run
embykeeper config.toml          # Full run with all modules
embykeeper config.toml -c       # Telegram check-ins only
embykeeper config.toml -e       # Emby keep-alive only
embykeeper config.toml -m       # Group monitoring only

# Test
pytest tests                    # Run all tests
pytest tests/test_emby_api.py   # Single test file
tox                             # Full matrix test (py38/39/310)

# Lint & Format
black --check .                 # Check formatting (line-length: 110)
black .                         # Auto-format
pre-commit run --all-files      # Run all pre-commit hooks
```

## Architecture

### Subsystem Pattern

Each Telegram subsystem follows the same structure under `embykeeper/telegram/`:

```
{subsystem}/
├── _base.py          # Abstract base class with core logic
├── _templ_*.py       # Template classes for quick bot creation
├── {bot_name}.py     # Individual bot implementations
└── test_*.py         # Tests for specific bots
```

Subsystems: `checkiner/` (50+ bots), `monitor/`, `messager/`, `registrar/`

### Key Modules

- `embykeeper/cli.py` — AsyncTyper CLI entry point
- `embykeeper/config.py` — TOML config with Pydantic validation and file watching
- `embykeeper/emby/api.py` — Emby server API client (login, playback simulation)
- `embykeeper/telegram/pyrogram.py` — Custom Pyrogram client wrapper
- `embykeeper/telegram/session.py` — Telegram session management with encryption
- `embykeeper/telegram/link.py` — Telegram link/invite parsing
- `embykeeper/ocr.py` — CAPTCHA solving via ddddocr

### Adding a New Check-in Bot

Create a file in `embykeeper/telegram/checkiner/` inheriting from a template:
- `_templ_a.py` — Simple message-based check-in
- `_templ_b.py` — Button/callback-based check-in  
- `_templ_c.py` — CAPTCHA-based check-in

Set class attributes (`name`, `bot_username`, `bot_checkin_cmd`, etc.) and the bot is auto-discovered.

## Code Style

- Formatter: Black, line-length 110
- All code comments in Chinese
- Async-first: use `asyncio` throughout
- Logging: `loguru` with scheme-bound loggers (`logger.bind(scheme="...")`)
- HTTP: `httpx` with HTTP/2 support
- Config: TOML format, validated by Pydantic models in `schema.py`
