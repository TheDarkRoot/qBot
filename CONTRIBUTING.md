# Contributing to qbot

Contributions, bug reports, and new locale translations are welcome.

## Design Principles

1. **Zero external Python dependencies:** Keep all runtime code strictly within the Python 3 standard library so `qbot` stays lightweight on Entware, OpenWrt, and SBCs.
2. **Low RAM footprint:** Avoid loading large modules or buffering big files in memory.
3. **Security hygiene:** Never log bot tokens, WebUI passwords, or private tracker passkeys. Keep `config.json` out of commits.

## Adding a New Language

1. Copy `locales/en.json` to `locales/<lang_code>.json`.
2. Translate all string values while keeping `{placeholders}` and HTML tags intact.
3. Validate JSON syntax:
   ```bash
   python3 -c "import json; json.load(open('locales/<lang_code>.json', encoding='utf-8'))"
   ```
4. Open a Pull Request describing your changes.