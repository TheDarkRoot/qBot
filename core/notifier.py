from __future__ import annotations

import logging
import threading
from typing import Iterable, Set

from api.qbittorrent import QBittorrentClient, QBittorrentError
from core.formatters import format_size
from core.i18n import Translator
from telegram.client import TelegramClient, TelegramError, escape_html


class CompletionNotifier:
    """Lightweight background watcher that notifies admins when torrents finish."""

    def __init__(
        self,
        qbt: QBittorrentClient,
        tg: TelegramClient,
        translator: Translator,
        notify_chat_ids: Iterable[int],
        stop_event: threading.Event,
        interval_sec: int = 30,
    ) -> None:
        self._qbt = qbt
        self._tg = tg
        self._tr = translator
        self._chat_ids = list(notify_chat_ids)
        self._stop_event = stop_event
        self._interval = max(10, int(interval_sec))
        self._logger = logging.getLogger("qbot.notifier")
        self._notified: Set[str] = set()
        self._initialized = False
        self._thread = threading.Thread(
            target=self._run, name="qbot-notifier", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @staticmethod
    def _is_completed(torrent: dict) -> bool:
        size = int(torrent.get("size", 0) or 0)
        progress = float(torrent.get("progress", 0.0) or 0.0)
        amount_left = int(torrent.get("amount_left", 1) or 0)
        return size > 0 and (progress >= 0.9999 or amount_left == 0)

    def _poll_once(self) -> None:
        torrents = self._qbt.get_torrents()
        current_hashes = set()

        for item in torrents:
            t_hash = str(item.get("hash", ""))
            if not t_hash:
                continue
            current_hashes.add(t_hash)

            if not self._is_completed(item):
                continue

            if not self._initialized:
                # Acilista zaten tamamlanmis torrentleri sessizce kaydet
                self._notified.add(t_hash)
                continue

            if t_hash not in self._notified:
                self._notified.add(t_hash)
                self._send_notification(item)

        # Silinen torrentleri bellekten temizle (RAM korumasi)
        self._notified.intersection_update(current_hashes)
        self._initialized = True

    def _send_notification(self, torrent: dict) -> None:
        text = self._tr.t(
            "notify_completed",
            name=escape_html(torrent.get("name", "Unknown")),
            size=format_size(torrent.get("size", 0)),
            ratio=f"{float(torrent.get('ratio', 0.0)):.2f}",
            save_path=escape_html(torrent.get("save_path", "")),
        )
        for chat_id in self._chat_ids:
            try:
                self._tg.send_message(chat_id, text)
            except TelegramError as exc:
                self._logger.warning(
                    "Failed to send completion alert to %s: %s", chat_id, exc
                )

    def _run(self) -> None:
        self._logger.info("Completion notifier started (interval: %ss).", self._interval)
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except QBittorrentError as exc:
                self._logger.debug("Notifier poll skipped (qBittorrent error): %s", exc)
            except Exception as exc:
                self._logger.error("Unexpected notifier error: %s", exc)

            self._stop_event.wait(self._interval)
        self._logger.info("Completion notifier stopped.")