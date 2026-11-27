# qbot

**Zero-dependency, ultra-lightweight Telegram bot for managing qBittorrent (v4.x & v5.x) on routers, SBCs, NAS devices, and Linux/Windows servers.**

Built exclusively with the Python 3 standard library (`urllib`, `json`, `ssl`, `http.cookiejar`), `qbot` runs 24/7 with ~25 MB RAM on MIPS/ARM routers (Entware / Keenetic / OpenWrt), Raspberry Pi, Docker, or standard Linux/Windows hosts.

## Features

- **Zero External Dependencies:** Pure Python 3 standard library — no `pip`, no virtualenvs, no compiled wheels.
- **Full qBittorrent Control:** Add magnet links or `.torrent` files directly in chat with a pre-add wizard (category, sequential download, start paused).
- **Selective File Downloads:** Paginated file picker to toggle individual file priorities or select/deselect all.
- **Speed Management:** Global & per-torrent speed limits, one-tap Turtle (Alt Speed) mode, and automatic **Day/Night speed profiles** (`/schedule`).
- **Monitoring & RSS:** Completion notifications, passkey-masked tracker diagnostics, `/sysinfo` (RAM, Swap/zRAM, disk usage, optional `smartctl` S.M.A.R.T. health), and `/rss` feed management.
- **Security First:** Role-based access control (Admin / User), automatic secret masking in logs, `chmod 600` config permission checks, and double-confirmation before deleting files.
- **Multilingual:** English (`en`) and Turkish (`tr`) built-in, easily extensible via `locales/*.json`.

## Quick Start

### Option 1: One-Line Installer (Entware / OpenWrt / Linux)

```sh
curl -fsSL [https://raw.githubusercontent.com/TheDarkRoot/qBot/main/install.sh](https://raw.githubusercontent.com/TheDarkRoot/qBot/main/install.sh) | sh
```

### Option 2: Manual Setup (Linux / Windows / macOS)

```bash
git clone [https://github.com/TheDarkRoot/qBot.git](https://github.com/TheDarkRoot/qBot.git)
cd qBot
cp config.example.json config.json
# Edit config.json with your Telegram bot token, admin ID, and qBittorrent credentials
python3 main.py
```

### Option 3: Docker Compose

```bash
cp config.example.json config.json
# Edit config.json
docker compose up -d
```

## Configuration

Copy `config.example.json` to `config.json` and fill in:

- `telegram.bot_token` — Token from [@BotFather](https://t.me/BotFather)
- `telegram.admin_ids` — List of Telegram numeric user IDs with full control
- `telegram.user_ids` — Optional read/add-only user IDs
- `qbittorrent.url` — WebUI URL (e.g., `http://127.0.0.1:9080` on the same host)
- `qbittorrent.username` / `password` — qBittorrent WebUI credentials
- `bot.language` — `"en"` or `"tr"`
- `scheduler` — Optional automatic day/night global speed limits

> **Security Tip:** Never commit `config.json` to version control (it is excluded in `.gitignore`). On Linux/Entware, keep permissions restricted with `chmod 600 config.json`.

## Commands

| Command | Description | Access |
| :--- | :--- | :--- |
| `/status` | Global transfer speeds, Turtle mode & torrent summary | All |
| `/list` | Paginated torrent list with inline filters & detail cards | All |
| `/downloading`, `/seeding`, `/completed`, `/paused`, `/errored` | Direct filtered views | All |
| `/add <link>` | Open pre-add wizard for a magnet or `.torrent` URL | All |
| `/speed [dl\|ul <MB/s>]` | Global speed limits panel or custom limit (`0` = unlimited) | Admin |
| `/turtle` | Toggle Alternative Speed (Turtle) mode | Admin |
| `/schedule [on\|off\|apply]` | Day/night scheduled speed profiles | Admin |
| `/sysinfo` | Host uptime, load, RAM, Swap/zRAM, disk space & S.M.A.R.T. | Admin |
| `/rss [add\|del]` | List, add, or remove qBittorrent RSS feeds | Admin |
| `/pauseall`, `/resumeall` | Pause or resume all torrents | Admin |

## Tested Environments

- Keenetic Hero DSL (KN-2410) + Entware (`mipsel`) + qBittorrent v4.6.7
- Windows 11 + Python 3.12+
- Linux (`systemd`) & Alpine Docker

## License

MIT License — see [LICENSE](LICENSE) for details.