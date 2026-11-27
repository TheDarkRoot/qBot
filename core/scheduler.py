from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from api.qbittorrent import QBittorrentClient, QBittorrentError
from core.formatters import format_limit
from core.i18n import Translator
from telegram.client import TelegramClient, TelegramError


def _parse_hhmm(value: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
    try:
        parts = str(value).strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, IndexError):
        pass
    return fallback


class SpeedScheduler:
    """Zero-dependency day/night global speed profile scheduler."""

    def __init__(
        self,
        qbt: QBittorrentClient,
        tg: TelegramClient,
        translator: Translator,
        notify_chat_ids: Iterable[int],
        stop_event: threading.Event,
        cfg: Dict[str, Any],
    ) -> None:
        self._qbt = qbt
        self._tg = tg
        self._tr = translator
        self._chat_ids = list(notify_chat_ids)
        self._stop_event = stop_event
        self._logger = logging.getLogger("qbot.scheduler")
        self._lock = threading.RLock()

        self._enabled = bool(cfg.get("enabled", False))
        self._interval = max(15, int(cfg.get("check_interval", 60)))
        self._night_start = _parse_hhmm(cfg.get("night_start", "01:00"), (1, 0))
        self._night_end = _parse_hhmm(cfg.get("night_end", "07:00"), (7, 0))
        self._night_dl_kb = max(0, int(cfg.get("night_dl_limit_kb", 0)))
        self._night_ul_kb = max(0, int(cfg.get("night_ul_limit_kb", 0)))
        self._day_dl_kb = max(0, int(cfg.get("day_dl_limit_kb", 3072)))
        self._day_ul_kb = max(0, int(cfg.get("day_ul_limit_kb", 512)))
        self._notify = bool(cfg.get("notify_on_switch", False))

        self._active_profile: Optional[str] = None
        self._thread = threading.Thread(
            target=self._run, name="qbot-scheduler", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def current_window_profile(self, now: Optional[datetime] = None) -> str:
        cur = now or datetime.now()
        cur_min = cur.hour * 60 + cur.minute
        start_min = self._night_start[0] * 60 + self._night_start[1]
        end_min = self._night_end[0] * 60 + self._night_end[1]

        if start_min == end_min:
            return "day"
        if start_min < end_min:
            is_night = start_min <= cur_min < end_min
        else:
            # Gece yarisini asan araliklar (ornek: 23:00 -> 07:00)
            is_night = cur_min >= start_min or cur_min < end_min
        return "night" if is_night else "day"

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "current_profile": self.current_window_profile(),
                "applied_profile": self._active_profile,
                "night_start": f"{self._night_start[0]:02d}:{self._night_start[1]:02d}",
                "night_end": f"{self._night_end[0]:02d}:{self._night_end[1]:02d}",
                "night_dl_bytes": self._night_dl_kb * 1024,
                "night_ul_bytes": self._night_ul_kb * 1024,
                "day_dl_bytes": self._day_dl_kb * 1024,
                "day_ul_bytes": self._day_ul_kb * 1024,
            }

    def set_enabled(self, state: bool) -> bool:
        with self._lock:
            self._enabled = bool(state)
            if not self._enabled:
                self._active_profile = None
                return False
        self.apply_now(notify=False)
        return True

    def apply_now(self, notify: bool = False) -> str:
        with self._lock:
            profile = self.current_window_profile()
            dl_bytes = (
                self._night_dl_kb if profile == "night" else self._day_dl_kb
            ) * 1024
            ul_bytes = (
                self._night_ul_kb if profile == "night" else self._day_ul_kb
            ) * 1024

        self._qbt.set_global_download_limit(dl_bytes)
        self._qbt.set_global_upload_limit(ul_bytes)

        with self._lock:
            self._active_profile = profile

        self._logger.info(
            "Applied '%s' speed profile (DL=%s B/s, UL=%s B/s).",
            profile,
            dl_bytes,
            ul_bytes,
        )
        if notify and self._notify:
            self._send_switch_alert(profile, dl_bytes, ul_bytes)
        return profile

    def _send_switch_alert(self, profile: str, dl_bytes: int, ul_bytes: int) -> None:
        unlim = self._tr.t("unlimited")
        profile_lbl = self._tr.t(
            "sched_profile_night" if profile == "night" else "sched_profile_day"
        )
        text = self._tr.t(
            "sched_switched",
            profile=profile_lbl,
            dl_limit=format_limit(dl_bytes, unlim),
            ul_limit=format_limit(ul_bytes, unlim),
        )
        for chat_id in self._chat_ids:
            try:
                self._tg.send_message(chat_id, text)
            except TelegramError as exc:
                self._logger.warning("Scheduler alert failed for %s: %s", chat_id, exc)

    def _run(self) -> None:
        self._logger.info(
            "Speed scheduler thread ready (enabled: %s, interval: %ss).",
            self._enabled,
            self._interval,
        )
        while not self._stop_event.is_set():
            try:
                if self.enabled:
                    target = self.current_window_profile()
                    if target != self._active_profile:
                        self.apply_now(notify=True)
            except QBittorrentError as exc:
                self._logger.debug("Scheduler tick skipped (qBittorrent error): %s", exc)
            except Exception as exc:
                self._logger.error("Unexpected scheduler error: %s", exc)

            self._stop_event.wait(self._interval)
        self._logger.info("Speed scheduler stopped.")