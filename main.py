from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from api.qbittorrent import QBittorrentClient, QBittorrentError
from core.auth import AuthManager, Role
from core.config import ConfigError, load_config
from core.i18n import Translator
from core.logger import setup_logger
from telegram.client import (
    TelegramAPIError,
    TelegramClient,
    TelegramError,
    TelegramNetworkError,
    escape_html,
)

BASE_DIR = Path(__file__).resolve().parent


def _format_speed(bytes_per_sec: int) -> str:
    if bytes_per_sec >= 1_048_576:
        return f"{bytes_per_sec / 1_048_576:.2f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec} B/s"


def _build_ping_message(
    bot_username: str, qbt: QBittorrentClient, tr: Translator
) -> tuple[str, Dict[str, Any]]:
    app_ver = qbt.get_app_version()
    transfer = qbt.get_transfer_info()
    torrents = qbt.get_torrents()
    text = tr.t(
        "ping_msg",
        username=escape_html(bot_username),
        qbt_ver=escape_html(app_ver),
        count=len(torrents),
        dl=_format_speed(transfer.get("dl_info_speed", 0)),
        ul=_format_speed(transfer.get("up_info_speed", 0)),
        now=datetime.now().strftime("%H:%M:%S"),
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": f"🔄 {tr.t('btn_refresh')}", "callback_data": "ping:refresh"}]
        ]
    }
    return text, keyboard


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
        tg.set_my_commands(
            [
                {"command": "start", "description": "Bot durumu ve yetki bilgisi"},
                {"command": "ping", "description": "Canlı qBittorrent bağlantı testi"},
            ]
        )
        logger.info("Connected to Telegram as @%s (id=%s)", bot_username, me.get("id"))
    except TelegramError as exc:
        logger.error("Telegram connection check failed: %s", exc)
        return 1

    # Temiz kapanis bayragi (Ctrl+C / Entware init.d stop)
    running = True

    def _handle_stop(signum: int, _frame: Any) -> None:
        nonlocal running
        logger.info("Shutdown signal received (%s); stopping qbot...", signum)
        running = False

    signal.signal(signal.SIGINT, _handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_stop)

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

    while running:
        try:
            updates = tg.get_updates(offset=offset, timeout=poll_timeout)
        except TelegramAPIError as exc:
            if exc.error_code == 409:
                logger.error(
                    "Conflict (409): Another instance of this bot is already running."
                )
                time.sleep(10)
            elif exc.retry_after:
                logger.warning("Rate limited by Telegram; waiting %ss...", exc.retry_after)
                time.sleep(exc.retry_after)
            else:
                logger.error("Telegram API error during polling: %s", exc)
                time.sleep(5)
            continue
        except TelegramNetworkError as exc:
            logger.warning("%s — retrying in 5s...", exc)
            time.sleep(5)
            continue
        except KeyboardInterrupt:
            break

        for update in updates:
            offset = int(update["update_id"]) + 1
            try:
                if "message" in update:
                    msg = update["message"]
                    from_user = msg.get("from") or {}
                    user_id = from_user.get("id")
                    chat_id = (msg.get("chat") or {}).get("id")
                    text = (msg.get("text") or "").strip()
                    if chat_id is None or user_id is None:
                        continue

                    role = auth.get_role(user_id)
                    if role is Role.UNAUTHORIZED:
                        if auth.should_notify_unauthorized(user_id):
                            logger.warning(
                                "Unauthorized message blocked from user_id=%s", user_id
                            )
                            tg.send_message(
                                chat_id, tr.t("unauthorized", user_id=user_id)
                            )
                        continue

                    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
                    if cmd == "/start":
                        role_label = (
                            tr.t("role_admin")
                            if role is Role.ADMIN
                            else tr.t("role_user")
                        )
                        torrents = qbt.get_torrents()
                        tg.send_message(
                            chat_id,
                            tr.t(
                                "welcome_msg",
                                role=role_label,
                                user_id=user_id,
                                qbt_ver=escape_html(qbt.get_app_version()),
                                count=len(torrents),
                            ),
                        )
                    elif cmd == "/ping":
                        ping_text, markup = _build_ping_message(bot_username, qbt, tr)
                        tg.send_message(chat_id, ping_text, reply_markup=markup)

                elif "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb["id"]
                    user_id = (cb.get("from") or {}).get("id")
                    if not auth.is_authorized(user_id):
                        tg.answer_callback_query(cb_id, text="Unauthorized", show_alert=True)
                        continue

                    data = cb.get("data", "")
                    cb_msg = cb.get("message") or {}
                    chat_id = (cb_msg.get("chat") or {}).get("id")
                    msg_id = cb_msg.get("message_id")

                    if data == "ping:refresh" and chat_id and msg_id:
                        ping_text, markup = _build_ping_message(bot_username, qbt, tr)
                        tg.edit_message_text(
                            chat_id, msg_id, ping_text, reply_markup=markup
                        )
                        tg.answer_callback_query(cb_id, text=tr.t("callback_refreshed"))
            except (TelegramError, QBittorrentError) as exc:
                logger.error("Error handling update %s: %s", update.get("update_id"), exc)

    logger.info("qbot stopped cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())