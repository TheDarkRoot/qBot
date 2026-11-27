from __future__ import annotations

import os
import sys
from pathlib import Path

from core.config import ConfigError, load_config
from core.i18n import Translator
from core.logger import setup_logger

BASE_DIR = Path(__file__).resolve().parent


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
    logger.info("qBittorrent target URL: %s", cfg["qbittorrent"]["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())