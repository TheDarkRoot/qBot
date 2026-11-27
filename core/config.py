from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Tuple


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def _parse_int_list(value: str) -> List[int]:
    result: List[int] = []
    for item in value.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        try:
            result.append(int(cleaned))
        except ValueError as exc:
            raise ConfigError(
                f"Invalid integer ID in environment variable: '{cleaned}'"
            ) from exc
    return result


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


DEFAULT_CONFIG: Dict[str, Any] = {
    "telegram": {
        "bot_token": "",
        "admin_ids": [],
        "user_ids": [],
        "poll_timeout": 30,
    },
    "qbittorrent": {
        "url": "http://127.0.0.1:9080",
        "username": "admin",
        "password": "",
        "verify_ssl": True,
        "timeout": 15,
        "default_save_path": "",
    },
    "bot": {
        "language": "tr",
        "items_per_page": 5,
        "notify_on_complete": True,
        "notify_interval": 30,
        "log_level": "INFO",
        "log_to_file": False,
        "log_file": "logs/qbot.log",
    },
    "scheduler": {
        "enabled": False,
        "check_interval": 60,
        "night_start": "01:00",
        "night_end": "07:00",
        "night_dl_limit_kb": 0,
        "night_ul_limit_kb": 0,
        "day_dl_limit_kb": 3072,
        "day_ul_limit_kb": 512,
        "notify_on_switch": False,
    },
    "monitor": {
        "enabled": False,
        "disk_paths": [],
        "smart_devices": ["auto"],
        "disk_warn_percent": 90,
        "temp_warn_celsius": 50,
    },
}


def load_config(config_path: Path) -> Tuple[Dict[str, Any], List[str]]:
    """Load config from JSON file and apply QBOT_* ENV overrides."""
    warnings: List[str] = []
    file_data: Dict[str, Any] = {}

    if config_path.exists():
        # POSIX sistemlerde (Entware, OpenWrt, Linux, RPi) dosya izni denetimi
        if os.name == "posix":
            file_mode = stat.S_IMODE(config_path.stat().st_mode)
            if file_mode & 0o077:
                warnings.append(
                    f"Security warning: '{config_path}' is readable by group/others "
                    f"(mode {oct(file_mode)}). Run: chmod 600 {config_path}"
                )
        try:
            with config_path.open("r", encoding="utf-8") as fh:
                file_data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON in '{config_path}': {exc}") from exc
    elif not os.getenv("QBOT_BOT_TOKEN"):
        raise ConfigError(
            f"Config file '{config_path}' not found. Copy 'config.example.json' to "
            f"'config.json' or set QBOT_* environment variables."
        )

    cfg = _deep_merge(DEFAULT_CONFIG, file_data)

    # Docker ve systemd icin ortam degiskeni (ENV) gecersiz kilmalari
    env_map = [
        ("QBOT_BOT_TOKEN", ("telegram", "bot_token"), str),
        ("QBOT_ADMIN_IDS", ("telegram", "admin_ids"), _parse_int_list),
        ("QBOT_USER_IDS", ("telegram", "user_ids"), _parse_int_list),
        ("QBOT_QBT_URL", ("qbittorrent", "url"), str),
        ("QBOT_QBT_USERNAME", ("qbittorrent", "username"), str),
        ("QBOT_QBT_PASSWORD", ("qbittorrent", "password"), str),
        ("QBOT_QBT_VERIFY_SSL", ("qbittorrent", "verify_ssl"), _parse_bool),
        ("QBOT_DEFAULT_SAVE_PATH", ("qbittorrent", "default_save_path"), str),
        ("QBOT_LANGUAGE", ("bot", "language"), str),
        ("QBOT_LOG_LEVEL", ("bot", "log_level"), str),
    ]
    for env_key, (section, field), parser in env_map:
        raw_val = os.getenv(env_key)
        if raw_val is not None and raw_val.strip() != "":
            cfg[section][field] = parser(raw_val)

    _validate_config(cfg)
    return cfg, warnings


def _validate_config(cfg: Dict[str, Any]) -> None:
    token = str(cfg["telegram"].get("bot_token", "")).strip()
    if (
        not token
        or "PUT_YOUR_BOT_TOKEN" in token
        or "YOUR_TELEGRAM_BOT_TOKEN" in token
        or ":" not in token
    ):
        raise ConfigError("A valid Telegram 'bot_token' is required.")

    admin_ids = cfg["telegram"].get("admin_ids", [])
    if not isinstance(admin_ids, list) or not admin_ids:
        raise ConfigError("At least one numeric ID must be set in 'telegram.admin_ids'.")
    if not all(isinstance(uid, int) and not isinstance(uid, bool) for uid in admin_ids):
        raise ConfigError("All entries in 'telegram.admin_ids' must be integers.")

    user_ids = cfg["telegram"].get("user_ids", [])
    if not isinstance(user_ids, list) or not all(
        isinstance(uid, int) and not isinstance(uid, bool) for uid in user_ids
    ):
        raise ConfigError("All entries in 'telegram.user_ids' must be integers.")

    poll_timeout = cfg["telegram"].get("poll_timeout", 30)
    if not isinstance(poll_timeout, int) or not (5 <= poll_timeout <= 60):
        raise ConfigError("'telegram.poll_timeout' must be an integer between 5 and 60.")

    qbt_url = str(cfg["qbittorrent"].get("url", "")).strip().rstrip("/")
    if not (qbt_url.startswith("http://") or qbt_url.startswith("https://")):
        raise ConfigError("'qbittorrent.url' must start with http:// or https://")
    cfg["qbittorrent"]["url"] = qbt_url