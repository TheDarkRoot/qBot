from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from api.qbittorrent import QBittorrentClient, QBittorrentError
from core.auth import AuthManager
from core.config import ConfigError, load_config
from core.i18n import Translator
from core.logger import setup_logger
from core.notifier import CompletionNotifier
from handlers.router import BotRouter
from telegram.client import (
    TelegramAPIError,
    TelegramClient,
    TelegramError,
    TelegramNetworkError,
)

BASE_DIR = Path(__file__).resolve().parent


def _register_commands(tg: TelegramClient, tr: Translator) -> None:
    tg.set_my_commands(
        [
            {"command": "status", "description": tr.t("cmd_status")},
            {"command": "speed", "description": tr.t("cmd_speed")},
            {"command": "turtle", "description": tr.t("cmd_turtle")},
            {"command": "list", "description": tr.t("cmd_list")},
            {"command": "downloading", "description": tr.t("cmd_downloading")},
            {"command": "seeding", "description": tr.t("cmd_seeding")},
            {"command": "completed", "description": tr.t("cmd_completed")},
            {"command": "paused", "description": tr.t("cmd_paused")},
            {"command": "errored", "description": tr.t("cmd_errored")},
            {"command": "help", "description": tr.t("cmd_help")},
            {"command": "start", "description": tr.t("cmd_start")},
            {"command": "sysinfo", "description": tr.t("cmd_sysinfo")},
            {"command": "rss", "description": tr.t("cmd_rss")},
        ]
    )


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

    tr = Translator(BASE_DIR / "locales", cfg["bot"]["language"])
    auth = AuthManager(
        admin_ids=cfg["telegram"]["admin_ids"],
        user_ids=cfg["telegram"]["user_ids"],
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
        qbt_ver = qbt.get_app_version()
        logger.info("Connected to qBittorrent %s (v5 mode: %s)", qbt_ver, qbt.is_v5)
    except QBittorrentError as exc:
        logger.error("qBittorrent connection check failed: %s", exc)
        return 1

    tg = TelegramClient(bot_token=cfg["telegram"]["bot_token"])
    try:
        me = tg.get_me()
        bot_username = str(me.get("username", "qbot"))
        _register_commands(tg, tr)
        logger.info("Connected to Telegram as @%s (id=%s)", bot_username, me.get("id"))
    except TelegramError as exc:
        logger.error("Telegram connection check failed: %s", exc)
        return 1

    router = BotRouter(
        tg=tg,
        qbt=qbt,
        auth=auth,
        translator=tr,
        default_save_path=qbt_cfg.get("default_save_path", ""),
        items_per_page=cfg["bot"].get("items_per_page", 5),
    )

    stop_event = threading.Event()

    def _handle_stop(signum: int, _frame: Any) -> None:
        logger.info("Shutdown signal received (%s); stopping qbot...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_stop)

    notifier: Optional[CompletionNotifier] = None
    if cfg["bot"].get("notify_on_complete", True):
        notifier = CompletionNotifier(
            qbt=qbt,
            tg=tg,
            translator=tr,
            notify_chat_ids=cfg["telegram"]["admin_ids"],
            stop_event=stop_event,
            interval_sec=cfg["bot"].get("notify_interval", 30),
        )
        notifier.start()

    logger.info(
        tr.t(
            "bot_started",
            lang=cfg["bot"]["language"],
            admins=len(cfg["telegram"]["admin_ids"]),
            users=len(cfg["telegram"]["user_ids"]),
        )
    )

    offset: Optional[int] = None
    poll_timeout = int(cfg["telegram"]["poll_timeout"])

    while not stop_event.is_set():
        try:
            updates = tg.get_updates(offset=offset, timeout=poll_timeout)
        except TelegramAPIError as exc:
            if exc.error_code == 409:
                logger.error("Conflict (409): Another qbot instance is already running.")
                stop_event.wait(10)
            elif exc.retry_after:
                logger.warning("Rate limited by Telegram; waiting %ss...", exc.retry_after)
                stop_event.wait(exc.retry_after)
            else:
                logger.error("Telegram API error during polling: %s", exc)
                stop_event.wait(5)
            continue
        except TelegramNetworkError as exc:
            logger.warning("%s — retrying in 5s...", exc)
            stop_event.wait(5)
            continue
        except KeyboardInterrupt:
            stop_event.set()
            break

        for update in updates:
            offset = int(update["update_id"]) + 1
            try:
                router.handle_update(update)
            except (TelegramError, QBittorrentError) as exc:
                logger.error("Update %s failed: %s", update.get("update_id"), exc)
            except Exception as exc:
                logger.exception("Unexpected error handling update %s: %s", update.get("update_id"), exc)

    if notifier is not None:
        notifier.join(timeout=5.0)
    logger.info("qbot stopped cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())