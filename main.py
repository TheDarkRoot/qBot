from __future__ import annotations

import os
import sys
from pathlib import Path

from api.qbittorrent import QBittorrentClient, QBittorrentError
from core.config import ConfigError, load_config
from core.i18n import Translator
from core.logger import setup_logger

BASE_DIR = Path(__file__).resolve().parent


def _format_speed(bytes_per_sec: int) -> str:
    if bytes_per_sec >= 1_048_576:
        return f"{bytes_per_sec / 1_048_576:.2f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec} B/s"


def main() -> int:
    config_path = Path(os.getenv("QBOT_CONFIG_PATH", str(BASE_DIR / "config.json")))

    try:
        cfg, warnings = load_config(config_path)
    except ConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 1

    secrets = [
        cfg["telegram"]["bot_token"],
        cfg["qbittorrent"]["password"],
    ]
    log_file = BASE_DIR / cfg["bot"]["log_file"]
    logger = setup_logger(
        level_name=cfg["bot"]["log_level"],
        secrets=secrets,
        log_to_file=cfg["bot"]["log_to_file"],
        log_file=log_file,
    )

    for warning in warnings:
        logger.warning(warning)

    translator = Translator(BASE_DIR / "locales", cfg["bot"]["language"])
    logger.info(
        translator.t(
            "bot_started",
            lang=cfg["bot"]["language"],
            admins=len(cfg["telegram"]["admin_ids"]),
            users=len(cfg["telegram"]["user_ids"]),
        )
    )

    qbt_cfg = cfg["qbittorrent"]
    qbt = QBittorrentClient(
        base_url=qbt_cfg["url"],
        username=qbt_cfg["username"],
        password=qbt_cfg["password"],
        verify_ssl=qbt_cfg["verify_ssl"],
        timeout=qbt_cfg["timeout"],
    )

    try:
        qbt.login()
        app_ver = qbt.get_app_version()
        api_ver = qbt.get_webapi_version()
        transfer = qbt.get_transfer_info()
        torrents = qbt.get_torrents()
    except QBittorrentError as exc:
        logger.error("qBittorrent connection check failed: %s", exc)
        return 1

    logger.info(
        "Connected to qBittorrent %s (WebAPI %s, v5 mode: %s)",
        app_ver,
        api_ver,
        qbt.is_v5,
    )
    logger.info(
        "Torrents: %d | DL: %s | UL: %s",
        len(torrents),
        _format_speed(transfer.get("dl_info_speed", 0)),
        _format_speed(transfer.get("up_info_speed", 0)),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())