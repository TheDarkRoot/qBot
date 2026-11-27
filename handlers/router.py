from __future__ import annotations

import logging
import math
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from api.qbittorrent import QBittorrentClient, QBittorrentError
from core.auth import AuthManager, Role
from core.formatters import (
    format_eta,
    format_limit,
    format_size,
    format_speed,
    format_state,
    format_tracker_status,
    is_paused_state,
    progress_bar,
    safe_category,
    safe_tracker_host,
    truncate,
)
from core.i18n import Translator
from core.scheduler import SpeedScheduler
from monitor.system_info import collect_system_snapshot
from telegram.client import TelegramClient, TelegramError, escape_html

_MAGNET_RE = re.compile(r"magnet:\?xt=urn:[^\s]+", re.IGNORECASE)
_TORRENT_URL_RE = re.compile(r"https?://[^\s]+\.torrent(?:\?[^\s]*)?", re.IGNORECASE)

_FILTERS: Dict[str, Tuple[Optional[str], str]] = {
    "all": (None, "filter_all"),
    "dl": ("downloading", "filter_dl"),
    "up": ("seeding", "filter_up"),
    "cp": ("completed", "filter_cp"),
    "ps": ("paused", "filter_ps"),
    "er": ("errored", "filter_er"),
}

# Global ve torrent bazli hiz limit on ayarlari (KB/s cinsinden, 0 = sinirsiz)
_GLOBAL_DL_PRESETS_KB = [0, 1024, 2048, 5120, 10240]
_GLOBAL_UL_PRESETS_KB = [0, 256, 512, 1024, 2048]
_TORRENT_PRESETS_KB = [0, 512, 1024, 3072, 5120]

_FILES_PER_PAGE = 6
_PENDING_TTL_SEC = 600
_MAX_PENDING_ADDS = 10


class BotRouter:
    """Routes Telegram messages and inline callback queries to qBittorrent actions."""

    def __init__(
        self,
        tg: TelegramClient,
        qbt: QBittorrentClient,
        auth: AuthManager,
        translator: Translator,
        default_save_path: str = "",
        items_per_page: int = 5,
        scheduler: Optional[SpeedScheduler] = None,
        disk_paths: Optional[List[str]] = None,
        smart_devices: Optional[List[str]] = None,
    ) -> None:
        self._tg = tg
        self._qbt = qbt
        self._auth = auth
        self._tr = translator
        self._default_save_path = default_save_path
        self._per_page = max(1, min(15, int(items_per_page)))
        self._scheduler = scheduler
        self._disk_paths = list(disk_paths or [])
        self._smart_devices = (
            list(smart_devices) if smart_devices is not None else ["auto"]
        )
        self._logger = logging.getLogger("qbot.router")
        self._pending_adds: Dict[str, Dict[str, Any]] = {}

    def handle_update(self, update: Dict[str, Any]) -> None:
        if "message" in update:
            self._handle_message(update["message"])
        elif "callback_query" in update:
            self._handle_callback(update["callback_query"])

    # =========================================================================
    # MESAJ ISLEME (Komutlar, Magnet Linkleri, .torrent Dosyalari)
    # =========================================================================

    def _handle_message(self, msg: Dict[str, Any]) -> None:
        from_user = msg.get("from") or {}
        user_id = from_user.get("id")
        chat_id = (msg.get("chat") or {}).get("id")
        if user_id is None or chat_id is None:
            return

        role = self._auth.get_role(user_id)
        if role is Role.UNAUTHORIZED:
            if self._auth.should_notify_unauthorized(user_id):
                self._logger.warning("Unauthorized message blocked from user_id=%s", user_id)
                self._tg.send_message(
                    chat_id, self._tr.t("unauthorized", user_id=user_id)
                )
            return

        document = msg.get("document")
        if isinstance(document, dict):
            self._handle_torrent_document(chat_id, user_id, document)
            return

        text = (msg.get("text") or "").strip()
        if not text:
            return

        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].split("@")[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            self._dispatch_command(chat_id, user_id, role, cmd, arg)
            return

        magnets = _MAGNET_RE.findall(text)
        torrent_urls = _TORRENT_URL_RE.findall(text)
        links = magnets + [u for u in torrent_urls if u not in magnets]
        if links:
            self._open_preadd_for_links(chat_id, user_id, links)

    def _dispatch_command(
        self, chat_id: int, user_id: int, role: Role, cmd: str, arg: str
    ) -> None:
        try:
            if cmd == "/start":
                role_label = (
                    self._tr.t("role_admin")
                    if role is Role.ADMIN
                    else self._tr.t("role_user")
                )
                torrents = self._qbt.get_torrents()
                self._tg.send_message(
                    chat_id,
                    self._tr.t(
                        "welcome_msg",
                        role=role_label,
                        user_id=user_id,
                        qbt_ver=escape_html(self._qbt.get_app_version()),
                        count=len(torrents),
                    ),
                )
            elif cmd == "/help":
                self._tg.send_message(chat_id, self._tr.t("help_msg"))
            elif cmd == "/status":
                text, markup = self._render_status(is_admin=(role is Role.ADMIN))
                self._tg.send_message(chat_id, text, reply_markup=markup)
            elif cmd in ("/list", "/all"):
                self._send_list(chat_id, "all", 0)
            elif cmd in ("/downloading", "/dl"):
                self._send_list(chat_id, "dl", 0)
            elif cmd in ("/seeding", "/up"):
                self._send_list(chat_id, "up", 0)
            elif cmd in ("/completed", "/done"):
                self._send_list(chat_id, "cp", 0)
            elif cmd in ("/paused", "/stopped"):
                self._send_list(chat_id, "ps", 0)
            elif cmd in ("/errored", "/errors"):
                self._send_list(chat_id, "er", 0)
            elif cmd == "/add":
                if not arg:
                    self._tg.send_message(chat_id, self._tr.t("help_msg"))
                    return
                self._open_preadd_for_links(chat_id, user_id, [arg])
            elif cmd == "/sysinfo":
                if role is not Role.ADMIN:
                    self._tg.send_message(chat_id, self._tr.t("admin_only"))
                    return
                self._send_sysinfo(chat_id)
            elif cmd == "/rss":
                if role is not Role.ADMIN:
                    self._tg.send_message(chat_id, self._tr.t("admin_only"))
                    return
                self._handle_rss_command(chat_id, arg)
            elif cmd == "/schedule":
                if role is not Role.ADMIN:
                    self._tg.send_message(chat_id, self._tr.t("admin_only"))
                    return
                self._handle_schedule_command(chat_id, arg)
            elif cmd == "/speed":
                if role is not Role.ADMIN:
                    self._tg.send_message(chat_id, self._tr.t("admin_only"))
                    return
                self._handle_speed_command(chat_id, arg)
            elif cmd == "/turtle":
                if role is not Role.ADMIN:
                    self._tg.send_message(chat_id, self._tr.t("admin_only"))
                    return
                self._qbt.toggle_speed_limits_mode()
                active = self._qbt.get_speed_limits_mode()
                key = "turtle_toggled_on" if active else "turtle_toggled_off"
                self._tg.send_message(chat_id, self._tr.t(key))
            elif cmd == "/pauseall":
                if role is not Role.ADMIN:
                    self._tg.send_message(chat_id, self._tr.t("admin_only"))
                    return
                self._qbt.pause_torrents("all")
                self._tg.send_message(chat_id, self._tr.t("action_paused_all"))
            elif cmd == "/resumeall":
                if role is not Role.ADMIN:
                    self._tg.send_message(chat_id, self._tr.t("admin_only"))
                    return
                self._qbt.resume_torrents("all")
                self._tg.send_message(chat_id, self._tr.t("action_resumed_all"))
        except QBittorrentError as exc:
            self._tg.send_message(
                chat_id, self._tr.t("api_error", error=escape_html(str(exc)))
            )

    def _handle_speed_command(self, chat_id: int, arg: str) -> None:
        if not arg:
            text, markup = self._render_speed_panel()
            self._tg.send_message(chat_id, text, reply_markup=markup)
            return

        tokens = arg.split()
        if len(tokens) != 2 or tokens[0].lower() not in ("dl", "ul"):
            self._tg.send_message(chat_id, self._tr.t("speed_usage"))
            return

        direction = tokens[0].lower()
        try:
            mb_val = float(tokens[1].replace(",", "."))
            if mb_val < 0:
                raise ValueError
        except ValueError:
            self._tg.send_message(chat_id, self._tr.t("speed_usage"))
            return

        limit_bytes = int(round(mb_val * 1_048_576))
        if direction == "dl":
            self._qbt.set_global_download_limit(limit_bytes)
        else:
            self._qbt.set_global_upload_limit(limit_bytes)

        text, markup = self._render_speed_panel()
        self._tg.send_message(chat_id, text, reply_markup=markup)

    # =========================================================================
    # EKLEME ONCESI HAZIRLIK (Kategori, Sirali Indirme, Duraklatilmis Basla)
    # =========================================================================

    def _prune_pending(self) -> None:
        now = time.monotonic()
        expired = [
            pid
            for pid, item in self._pending_adds.items()
            if (now - item["created_at"]) > _PENDING_TTL_SEC
        ]
        for pid in expired:
            del self._pending_adds[pid]
        while len(self._pending_adds) >= _MAX_PENDING_ADDS:
            oldest = min(
                self._pending_adds, key=lambda k: self._pending_adds[k]["created_at"]
            )
            del self._pending_adds[oldest]

    def _create_pending(
        self,
        user_id: int,
        kind: str,
        label: str,
        links: Optional[List[str]] = None,
        filename: str = "",
        content: bytes = b"",
    ) -> str:
        self._prune_pending()
        pid = uuid.uuid4().hex[:8]
        try:
            cats = sorted(self._qbt.get_categories().keys())
        except QBittorrentError:
            cats = []
        self._pending_adds[pid] = {
            "user_id": user_id,
            "kind": kind,
            "label": label,
            "links": links or [],
            "filename": filename,
            "content": content,
            "category": "",
            "categories": cats[:16],
            "sequential": False,
            "paused": False,
            "created_at": time.monotonic(),
        }
        return pid

    def _open_preadd_for_links(
        self, chat_id: int, user_id: int, links: List[str]
    ) -> None:
        if len(links) == 1:
            label = self._extract_magnet_name(links[0])
        else:
            label = f"{len(links)} link"
        pid = self._create_pending(
            user_id=user_id, kind="links", label=label, links=links
        )
        text, markup = self._render_preadd(pid)
        self._tg.send_message(chat_id, text, reply_markup=markup)

    @staticmethod
    def _extract_magnet_name(link: str) -> str:
        match = re.search(r"[?&]dn=([^&]+)", link)
        if match:
            from urllib.parse import unquote_plus

            return truncate(unquote_plus(match.group(1)), 54)
        return truncate(link, 54)

    def _handle_torrent_document(
        self, chat_id: int, user_id: int, document: Dict[str, Any]
    ) -> None:
        filename = str(document.get("file_name") or "file.torrent")
        mime_type = str(document.get("mime_type") or "")
        file_size = int(document.get("file_size") or 0)

        if not (
            filename.lower().endswith(".torrent")
            or mime_type == "application/x-bittorrent"
        ):
            return

        if file_size > 2_097_152:
            self._tg.send_message(chat_id, self._tr.t("file_too_large"))
            return

        try:
            file_info = self._tg.get_file(str(document["file_id"]))
            file_path = str(file_info.get("file_path") or "")
            if not file_path:
                self._tg.send_message(chat_id, self._tr.t("add_failed"))
                return

            content = self._tg.download_file(file_path, max_size_bytes=2_097_152)
            pid = self._create_pending(
                user_id=user_id,
                kind="file",
                label=truncate(filename, 54),
                filename=filename,
                content=content,
            )
            text, markup = self._render_preadd(pid)
            self._tg.send_message(chat_id, text, reply_markup=markup)
        except TelegramError as exc:
            self._tg.send_message(
                chat_id, self._tr.t("api_error", error=escape_html(str(exc)))
            )

    def _render_preadd(self, pid: str) -> Tuple[str, Dict[str, Any]]:
        item = self._pending_adds[pid]
        cat_label = (
            escape_html(item["category"])
            if item["category"]
            else self._tr.t("cat_none")
        )
        seq_lbl = self._tr.t("on_label") if item["sequential"] else self._tr.t("off_label")
        ps_lbl = self._tr.t("on_label") if item["paused"] else self._tr.t("off_label")
        save_lbl = (
            escape_html(self._default_save_path)
            if self._default_save_path
            else "qBittorrent default"
        )

        text = self._tr.t(
            "preadd_msg",
            label=escape_html(item["label"]),
            category=cat_label,
            sequential=seq_lbl,
            paused=ps_lbl,
            save_path=save_lbl,
        )

        rows: List[List[Dict[str, str]]] = [
            [
                {
                    "text": f"🏷 {self._tr.t('btn_category')}: {item['category'] or '—'}",
                    "callback_data": f"pa:{pid}:cm",
                }
            ],
            [
                {
                    "text": f"{'✅' if item['sequential'] else '⬜'} {self._tr.t('btn_sequential')}",
                    "callback_data": f"pa:{pid}:sq",
                },
                {
                    "text": f"{'✅' if item['paused'] else '⬜'} {self._tr.t('btn_start_paused')}",
                    "callback_data": f"pa:{pid}:ps",
                },
            ],
            [
                {
                    "text": f"🚀 {self._tr.t('btn_confirm_add')}",
                    "callback_data": f"pa:{pid}:ok",
                },
                {
                    "text": f"❌ {self._tr.t('btn_cancel')}",
                    "callback_data": f"pa:{pid}:no",
                },
            ],
        ]
        return text, {"inline_keyboard": rows}

    def _render_preadd_categories(self, pid: str) -> Tuple[str, Dict[str, Any]]:
        item = self._pending_adds[pid]
        cats: List[str] = item["categories"]
        rows: List[List[Dict[str, str]]] = [
            [
                {
                    "text": f"{'• ' if not item['category'] else ''}{self._tr.t('cat_none')}",
                    "callback_data": f"pa:{pid}:cs:-1",
                }
            ]
        ]

        row: List[Dict[str, str]] = []
        for idx, cat in enumerate(cats):
            mark = "• " if item["category"] == cat else ""
            row.append(
                {
                    "text": f"{mark}{truncate(cat, 22)}",
                    "callback_data": f"pa:{pid}:cs:{idx}",
                }
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        rows.append(
            [{"text": f"⬅️ {self._tr.t('btn_back')}", "callback_data": f"pa:{pid}:bk"}]
        )
        return self._tr.t("preadd_cat_msg"), {"inline_keyboard": rows}

    def _handle_preadd_callback(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        user_id: int,
        role: Role,
        parts: List[str],
    ) -> None:
        self._prune_pending()
        pid = parts[1]
        act = parts[2]
        item = self._pending_adds.get(pid)
        if not item:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("preadd_expired"), show_alert=True
            )
            return

        if item["user_id"] != user_id and role is not Role.ADMIN:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("admin_only"), show_alert=True
            )
            return

        if act == "sq":
            item["sequential"] = not item["sequential"]
            text, markup = self._render_preadd(pid)
            self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
            self._tg.answer_callback_query(cb_id)
        elif act == "ps":
            item["paused"] = not item["paused"]
            text, markup = self._render_preadd(pid)
            self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
            self._tg.answer_callback_query(cb_id)
        elif act == "cm":
            text, markup = self._render_preadd_categories(pid)
            self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
            self._tg.answer_callback_query(cb_id)
        elif act == "cs" and len(parts) >= 4:
            val = parts[3]
            if val == "-1":
                item["category"] = ""
            elif val.isdigit() and int(val) < len(item["categories"]):
                item["category"] = item["categories"][int(val)]
            text, markup = self._render_preadd(pid)
            self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
            self._tg.answer_callback_query(cb_id)
        elif act == "bk":
            text, markup = self._render_preadd(pid)
            self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
            self._tg.answer_callback_query(cb_id)
        elif act == "no":
            del self._pending_adds[pid]
            self._tg.edit_message_text(chat_id, msg_id, self._tr.t("preadd_cancelled"))
            self._tg.answer_callback_query(cb_id)
        elif act == "ok":
            del self._pending_adds[pid]
            save_path = "" if item["category"] else self._default_save_path
            if item["kind"] == "links":
                ok = self._qbt.add_magnet(
                    urls="\n".join(item["links"]),
                    save_path=save_path,
                    category=item["category"],
                    paused=item["paused"],
                    sequential=item["sequential"],
                    first_last_piece_prio=item["sequential"],
                )
                done_msg = (
                    self._tr.t("added_magnet", count=len(item["links"]))
                    if ok
                    else self._tr.t("add_failed")
                )
            else:
                ok = self._qbt.add_torrent_file(
                    filename=item["filename"],
                    content=item["content"],
                    save_path=save_path,
                    category=item["category"],
                    paused=item["paused"],
                    sequential=item["sequential"],
                    first_last_piece_prio=item["sequential"],
                )
                done_msg = (
                    self._tr.t("added_file", filename=escape_html(item["filename"]))
                    if ok
                    else self._tr.t("add_failed")
                )
            self._tg.edit_message_text(chat_id, msg_id, done_msg)
            self._tg.answer_callback_query(cb_id)

    # =========================================================================
    # SATIR ICI BUTON (CALLBACK QUERY) YONLENDIRME
    # =========================================================================

    def _handle_callback(self, cb: Dict[str, Any]) -> None:
        cb_id = str(cb["id"])
        user_id = (cb.get("from") or {}).get("id")
        role = self._auth.get_role(user_id)
        if role is Role.UNAUTHORIZED:
            self._tg.answer_callback_query(cb_id, text="Unauthorized", show_alert=True)
            return

        data = str(cb.get("data") or "")
        cb_msg = cb.get("message") or {}
        chat_id = (cb_msg.get("chat") or {}).get("id")
        msg_id = cb_msg.get("message_id")
        if not chat_id or not msg_id:
            self._tg.answer_callback_query(cb_id)
            return

        parts = data.split(":")
        kind = parts[0] if parts else ""

        try:
            if kind == "pa" and len(parts) >= 3:
                self._handle_preadd_callback(
                    cb_id, chat_id, msg_id, int(user_id), role, parts
                )

            elif kind == "st":
                text, markup = self._render_status(is_admin=(role is Role.ADMIN))
                self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
                self._tg.answer_callback_query(cb_id, text=self._tr.t("callback_refreshed"))

            elif kind == "gl" and len(parts) >= 2:
                if role is not Role.ADMIN:
                    self._tg.answer_callback_query(
                        cb_id, text=self._tr.t("admin_only"), show_alert=True
                    )
                    return
                if parts[1] == "pa":
                    self._qbt.pause_torrents("all")
                    notice = self._tr.t("action_paused_all")
                elif parts[1] == "ra":
                    self._qbt.resume_torrents("all")
                    notice = self._tr.t("action_resumed_all")
                elif parts[1] == "tu":
                    self._qbt.toggle_speed_limits_mode()
                    active = self._qbt.get_speed_limits_mode()
                    notice = self._tr.t(
                        "turtle_toggled_on" if active else "turtle_toggled_off"
                    )
                else:
                    notice = ""
                text, markup = self._render_status(is_admin=True)
                self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
                self._tg.answer_callback_query(cb_id, text=notice)

            elif kind == "sp" and len(parts) >= 2:
                if role is not Role.ADMIN:
                    self._tg.answer_callback_query(
                        cb_id, text=self._tr.t("admin_only"), show_alert=True
                    )
                    return
                self._handle_speed_callback(cb_id, chat_id, msg_id, parts)

            elif kind == "sc" and len(parts) >= 2:
                if role is not Role.ADMIN:
                    self._tg.answer_callback_query(
                        cb_id, text=self._tr.t("admin_only"), show_alert=True
                    )
                    return
                self._handle_schedule_callback(cb_id, chat_id, msg_id, parts[1])

            elif kind == "ls" and len(parts) >= 3:
                flt = parts[1] if parts[1] in _FILTERS else "all"
                page = max(0, int(parts[2])) if parts[2].isdigit() else 0
                text, markup = self._render_list(flt, page)
                self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
                self._tg.answer_callback_query(cb_id)

            elif kind == "dt" and len(parts) >= 4:
                short_hash = parts[1]
                flt = parts[2] if parts[2] in _FILTERS else "all"
                page = max(0, int(parts[3])) if parts[3].isdigit() else 0
                self._show_detail(
                    cb_id, chat_id, msg_id, short_hash, flt, page, role is Role.ADMIN
                )

            elif kind == "fl" and len(parts) >= 5:
                short_hash, flt = parts[1], parts[2]
                page = max(0, int(parts[3])) if parts[3].isdigit() else 0
                fpage = max(0, int(parts[4])) if parts[4].isdigit() else 0
                self._show_files(
                    cb_id, chat_id, msg_id, short_hash, flt, page, fpage, role is Role.ADMIN
                )

            elif kind == "ft" and len(parts) >= 6:
                if role is not Role.ADMIN:
                    self._tg.answer_callback_query(
                        cb_id, text=self._tr.t("admin_only"), show_alert=True
                    )
                    return
                short_hash, flt = parts[1], parts[2]
                page = max(0, int(parts[3])) if parts[3].isdigit() else 0
                fpage = max(0, int(parts[4])) if parts[4].isdigit() else 0
                file_idx = int(parts[5]) if parts[5].isdigit() else -1
                self._toggle_file_priority(
                    cb_id, chat_id, msg_id, short_hash, flt, page, fpage, file_idx
                )

            elif kind == "fa" and len(parts) >= 6:
                if role is not Role.ADMIN:
                    self._tg.answer_callback_query(
                        cb_id, text=self._tr.t("admin_only"), show_alert=True
                    )
                    return
                mode = parts[1]
                short_hash, flt = parts[2], parts[3]
                page = max(0, int(parts[4])) if parts[4].isdigit() else 0
                fpage = max(0, int(parts[5])) if parts[5].isdigit() else 0
                self._set_all_files_priority(
                    cb_id, chat_id, msg_id, short_hash, flt, page, fpage, enable=(mode == "1")
                )

            elif kind == "tr" and len(parts) >= 4:
                short_hash, flt = parts[1], parts[2]
                page = max(0, int(parts[3])) if parts[3].isdigit() else 0
                self._show_trackers(
                    cb_id, chat_id, msg_id, short_hash, flt, page, role is Role.ADMIN
                )

            elif kind == "tl" and len(parts) >= 4:
                if role is not Role.ADMIN:
                    self._tg.answer_callback_query(
                        cb_id, text=self._tr.t("admin_only"), show_alert=True
                    )
                    return
                short_hash, flt = parts[1], parts[2]
                page = max(0, int(parts[3])) if parts[3].isdigit() else 0
                self._show_torrent_limits(cb_id, chat_id, msg_id, short_hash, flt, page)

            elif kind == "ts" and len(parts) >= 6:
                if role is not Role.ADMIN:
                    self._tg.answer_callback_query(
                        cb_id, text=self._tr.t("admin_only"), show_alert=True
                    )
                    return
                direction, kb_str, short_hash, flt = parts[1], parts[2], parts[3], parts[4]
                page = max(0, int(parts[5])) if parts[5].isdigit() else 0
                limit_bytes = int(kb_str) * 1024 if kb_str.isdigit() else 0
                self._apply_torrent_limit(
                    cb_id, chat_id, msg_id, direction, limit_bytes, short_hash, flt, page
                )

            elif kind == "ac" and len(parts) >= 5:
                if role is not Role.ADMIN:
                    self._tg.answer_callback_query(
                        cb_id, text=self._tr.t("admin_only"), show_alert=True
                    )
                    return
                action, short_hash, flt = parts[1], parts[2], parts[3]
                page = max(0, int(parts[4])) if parts[4].isdigit() else 0
                self._run_torrent_action(
                    cb_id, chat_id, msg_id, action, short_hash, flt, page
                )

            elif kind == "dc" and len(parts) >= 5:
                if role is not Role.ADMIN:
                    self._tg.answer_callback_query(
                        cb_id, text=self._tr.t("admin_only"), show_alert=True
                    )
                    return
                mode, short_hash, flt = parts[1], parts[2], parts[3]
                page = max(0, int(parts[4])) if parts[4].isdigit() else 0
                self._run_delete_confirmed(
                    cb_id, chat_id, msg_id, mode, short_hash, flt, page
                )
            else:
                self._tg.answer_callback_query(cb_id)

        except QBittorrentError as exc:
            self._tg.answer_callback_query(
                cb_id, text=str(exc)[:180], show_alert=True
            )

    # =========================================================================
    # DURUM VE GLOBAL HIZ LIMITI PANELLERI
    # =========================================================================

    def _render_status(self, is_admin: bool) -> Tuple[str, Dict[str, Any]]:
        app_ver = self._qbt.get_app_version()
        transfer = self._qbt.get_transfer_info()
        turtle_on = self._qbt.get_speed_limits_mode()
        torrents = self._qbt.get_torrents()

        dl_cnt = sum(
            1
            for t in torrents
            if t.get("state") in ("downloading", "forcedDL", "metaDL", "stalledDL")
        )
        up_cnt = sum(
            1 for t in torrents if t.get("state") in ("uploading", "forcedUP", "stalledUP")
        )
        ps_cnt = sum(1 for t in torrents if is_paused_state(str(t.get("state", ""))))
        er_cnt = sum(1 for t in torrents if t.get("state") in ("error", "missingFiles"))

        text = self._tr.t(
            "status_msg",
            qbt_ver=escape_html(app_ver),
            dl=format_speed(transfer.get("dl_info_speed", 0)),
            ul=format_speed(transfer.get("up_info_speed", 0)),
            dl_session=format_size(transfer.get("dl_info_data", 0)),
            ul_session=format_size(transfer.get("up_info_data", 0)),
            turtle=self._tr.t("turtle_on") if turtle_on else self._tr.t("turtle_off"),
            total=len(torrents),
            downloading=dl_cnt,
            seeding=up_cnt,
            paused=ps_cnt,
            errored=er_cnt,
        )

        rows: List[List[Dict[str, str]]] = [
            [
                {"text": f"📋 {self._tr.t('filter_all')}", "callback_data": "ls:all:0"},
                {"text": f"⬇️ {self._tr.t('filter_dl')}", "callback_data": "ls:dl:0"},
            ]
        ]
        if is_admin:
            turtle_state = self._tr.t("turtle_on") if turtle_on else self._tr.t("turtle_off")
            rows.append(
                [
                    {"text": f"🐢 {self._tr.t('btn_turtle_toggle')}: {turtle_state}", "callback_data": "gl:tu"},
                    {"text": f"⚡ {self._tr.t('btn_speed')}", "callback_data": "sp:m"},
                ]
            )
            rows.append(
                [
                    {"text": f"⏸ {self._tr.t('btn_pause_all')}", "callback_data": "gl:pa"},
                    {"text": f"▶️ {self._tr.t('btn_resume_all')}", "callback_data": "gl:ra"},
                ]
            )
        rows.append([{"text": f"🔄 {self._tr.t('btn_refresh')}", "callback_data": "st:ref"}])
        return text, {"inline_keyboard": rows}

    def _preset_label(self, kb: int) -> str:
        if kb == 0:
            return "∞"
        if kb >= 1024:
            return f"{kb // 1024}M"
        return f"{kb}K"

    def _render_speed_panel(self) -> Tuple[str, Dict[str, Any]]:
        transfer = self._qbt.get_transfer_info()
        turtle_on = self._qbt.get_speed_limits_mode()
        unlim = self._tr.t("unlimited")

        text = self._tr.t(
            "speed_msg",
            dl=format_speed(transfer.get("dl_info_speed", 0)),
            ul=format_speed(transfer.get("up_info_speed", 0)),
            dl_limit=format_limit(transfer.get("dl_rate_limit", 0), unlim),
            ul_limit=format_limit(transfer.get("up_rate_limit", 0), unlim),
            turtle=self._tr.t("turtle_on") if turtle_on else self._tr.t("turtle_off"),
        )

        dl_row = [
            {
                "text": f"⬇️ {self._preset_label(kb)}",
                "callback_data": f"sp:dl:{kb}",
            }
            for kb in _GLOBAL_DL_PRESETS_KB
        ]
        ul_row = [
            {
                "text": f"⬆️ {self._preset_label(kb)}",
                "callback_data": f"sp:ul:{kb}",
            }
            for kb in _GLOBAL_UL_PRESETS_KB
        ]
        turtle_state = self._tr.t("turtle_on") if turtle_on else self._tr.t("turtle_off")
        rows = [
            dl_row,
            ul_row,
            [{"text": f"🐢 {self._tr.t('btn_turtle_toggle')}: {turtle_state}", "callback_data": "sp:tu"}],
            [
                {"text": f"⬅️ {self._tr.t('btn_back')}", "callback_data": "st:ref"},
                {"text": f"🔄 {self._tr.t('btn_refresh')}", "callback_data": "sp:m"},
            ],
        ]
        return text, {"inline_keyboard": rows}

    def _handle_speed_callback(
        self, cb_id: str, chat_id: int, msg_id: int, parts: List[str]
    ) -> None:
        sub = parts[1]
        toast = self._tr.t("callback_refreshed")
        if sub == "dl" and len(parts) >= 3 and parts[2].isdigit():
            self._qbt.set_global_download_limit(int(parts[2]) * 1024)
            toast = self._tr.t("speed_updated")
        elif sub == "ul" and len(parts) >= 3 and parts[2].isdigit():
            self._qbt.set_global_upload_limit(int(parts[2]) * 1024)
            toast = self._tr.t("speed_updated")
        elif sub == "tu":
            self._qbt.toggle_speed_limits_mode()
            active = self._qbt.get_speed_limits_mode()
            toast = self._tr.t("turtle_toggled_on" if active else "turtle_toggled_off")

        text, markup = self._render_speed_panel()
        self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
        self._tg.answer_callback_query(cb_id, text=toast)

    # =========================================================================
    # LISTE VE DETAY GORUNUMLERI
    # =========================================================================

    def _send_list(self, chat_id: int, flt: str, page: int) -> None:
        text, markup = self._render_list(flt, page)
        self._tg.send_message(chat_id, text, reply_markup=markup)

    def _render_list(self, flt: str, page: int) -> Tuple[str, Dict[str, Any]]:
        api_filter, title_key = _FILTERS.get(flt, (None, "filter_all"))
        title = self._tr.t(title_key)
        torrents = self._qbt.get_torrents(filter_state=api_filter)
        total = len(torrents)

        rows: List[List[Dict[str, str]]] = []
        filter_row = []
        for code in ("all", "dl", "up", "ps", "er"):
            label = self._tr.t(_FILTERS[code][1])
            prefix = "• " if code == flt else ""
            filter_row.append(
                {"text": f"{prefix}{label}", "callback_data": f"ls:{code}:0"}
            )
        rows.append(filter_row)

        if total == 0:
            rows.append(
                [{"text": f"🔄 {self._tr.t('btn_refresh')}", "callback_data": f"ls:{flt}:0"}]
            )
            return self._tr.t("list_empty", title=title), {"inline_keyboard": rows}

        total_pages = max(1, math.ceil(total / self._per_page))
        page = max(0, min(page, total_pages - 1))
        start = page * self._per_page
        chunk = torrents[start : start + self._per_page]

        lines = [
            self._tr.t(
                "list_header",
                title=title,
                shown=len(chunk),
                total=total,
                page=page + 1,
                pages=total_pages,
            ),
            "",
        ]

        for idx, t in enumerate(chunk, start=start + 1):
            name = escape_html(truncate(str(t.get("name", "Unknown")), 48))
            pct = float(t.get("progress", 0.0)) * 100.0
            bar = progress_bar(float(t.get("progress", 0.0)), width=8)
            state_lbl = format_state(str(t.get("state", "")))
            dl_spd = format_speed(t.get("dlspeed", 0))
            ul_spd = format_speed(t.get("upspeed", 0))
            size_lbl = format_size(t.get("size", 0))

            lines.append(f"<b>{idx}.</b> {name}")
            lines.append(f"   {state_lbl} | <code>{bar}</code> <b>{pct:.1f}%</b> ({size_lbl})")
            lines.append(f"   ⬇️ {dl_spd}  ⬆️ {ul_spd}")
            lines.append("")

            short_hash = str(t.get("hash", ""))[:12]
            btn_label = f"{idx}. {truncate(str(t.get('name', '')), 26)} ({pct:.1f}%)"
            rows.append(
                [{"text": btn_label, "callback_data": f"dt:{short_hash}:{flt}:{page}"}]
            )

        nav_row: List[Dict[str, str]] = []
        if page > 0:
            nav_row.append(
                {"text": f"⬅️ {self._tr.t('btn_prev')}", "callback_data": f"ls:{flt}:{page - 1}"}
            )
        nav_row.append(
            {"text": f"🔄 {self._tr.t('btn_refresh')}", "callback_data": f"ls:{flt}:{page}"}
        )
        if page + 1 < total_pages:
            nav_row.append(
                {"text": f"{self._tr.t('btn_next')} ➡️", "callback_data": f"ls:{flt}:{page + 1}"}
            )
        rows.append(nav_row)

        return "\n".join(lines).strip(), {"inline_keyboard": rows}

    def _find_by_short_hash(self, short_hash: str) -> Optional[Dict[str, Any]]:
        for t in self._qbt.get_torrents():
            if str(t.get("hash", "")).startswith(short_hash):
                return t
        return None

    def _show_detail(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        short_hash: str,
        flt: str,
        page: int,
        is_admin: bool,
        toast: Optional[str] = None,
    ) -> None:
        torrent = self._find_by_short_hash(short_hash)
        if not torrent:
            text, markup = self._render_list(flt, page)
            self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("torrent_not_found"), show_alert=True
            )
            return

        state = str(torrent.get("state", ""))
        progress = float(torrent.get("progress", 0.0))
        text = self._tr.t(
            "detail_msg",
            name=escape_html(str(torrent.get("name", "Unknown"))),
            state=format_state(state),
            bar=progress_bar(progress, width=12),
            progress=f"{progress * 100.0:.1f}",
            downloaded=format_size(torrent.get("completed", 0)),
            size=format_size(torrent.get("size", 0)),
            dl=format_speed(torrent.get("dlspeed", 0)),
            ul=format_speed(torrent.get("upspeed", 0)),
            eta=format_eta(int(torrent.get("eta", -1))),
            ratio=f"{float(torrent.get('ratio', 0.0)):.2f}",
            seeds=f"{torrent.get('num_seeds', 0)}/{torrent.get('num_complete', 0)}",
            peers=f"{torrent.get('num_leechs', 0)}/{torrent.get('num_incomplete', 0)}",
            category=safe_category(torrent.get("category")),
            save_path=escape_html(str(torrent.get("save_path", ""))),
        )

        rows: List[List[Dict[str, str]]] = [
            [
                {
                    "text": f"📂 {self._tr.t('btn_files')}",
                    "callback_data": f"fl:{short_hash}:{flt}:{page}:0",
                },
                {
                    "text": f"📡 {self._tr.t('btn_trackers')}",
                    "callback_data": f"tr:{short_hash}:{flt}:{page}",
                },
            ]
        ]

        if is_admin:
            if is_paused_state(state):
                primary_btn = {
                    "text": f"▶️ {self._tr.t('btn_resume')}",
                    "callback_data": f"ac:rs:{short_hash}:{flt}:{page}",
                }
            else:
                primary_btn = {
                    "text": f"⏸ {self._tr.t('btn_pause')}",
                    "callback_data": f"ac:ps:{short_hash}:{flt}:{page}",
                }

            rows.append(
                [
                    primary_btn,
                    {
                        "text": f"⚡ {self._tr.t('btn_force')}",
                        "callback_data": f"ac:fs:{short_hash}:{flt}:{page}",
                    },
                ]
            )
            rows.append(
                [
                    {
                        "text": f"🔍 {self._tr.t('btn_recheck')}",
                        "callback_data": f"ac:rc:{short_hash}:{flt}:{page}",
                    },
                    {
                        "text": f"🎚 {self._tr.t('btn_limit')}",
                        "callback_data": f"tl:{short_hash}:{flt}:{page}",
                    },
                ]
            )
            rows.append(
                [
                    {
                        "text": f"🗑 {self._tr.t('btn_delete')}",
                        "callback_data": f"ac:dl:{short_hash}:{flt}:{page}",
                    }
                ]
            )

        rows.append(
            [
                {
                    "text": f"⬅️ {self._tr.t('btn_back')}",
                    "callback_data": f"ls:{flt}:{page}",
                },
                {
                    "text": f"🔄 {self._tr.t('btn_refresh')}",
                    "callback_data": f"dt:{short_hash}:{flt}:{page}",
                },
            ]
        )

        self._tg.edit_message_text(
            chat_id, msg_id, text, reply_markup={"inline_keyboard": rows}
        )
        self._tg.answer_callback_query(cb_id, text=toast)

    # =========================================================================
    # SECICI DOSYA INDIRME (v1.5)
    # =========================================================================

    def _show_files(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        short_hash: str,
        flt: str,
        page: int,
        fpage: int,
        is_admin: bool,
        toast: Optional[str] = None,
    ) -> None:
        torrent = self._find_by_short_hash(short_hash)
        if not torrent:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("torrent_not_found"), show_alert=True
            )
            return

        files = self._qbt.get_torrent_files(str(torrent["hash"]))
        t_name = escape_html(truncate(str(torrent.get("name", "Unknown")), 44))

        if not files:
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": f"⬅️ {self._tr.t('btn_back')}",
                            "callback_data": f"dt:{short_hash}:{flt}:{page}",
                        },
                        {
                            "text": f"🔄 {self._tr.t('btn_refresh')}",
                            "callback_data": f"fl:{short_hash}:{flt}:{page}:0",
                        },
                    ]
                ]
            }
            self._tg.edit_message_text(
                chat_id, msg_id, self._tr.t("files_empty", name=t_name), reply_markup=markup
            )
            self._tg.answer_callback_query(cb_id, text=toast)
            return

        total = len(files)
        total_pages = max(1, math.ceil(total / _FILES_PER_PAGE))
        fpage = max(0, min(fpage, total_pages - 1))
        start = fpage * _FILES_PER_PAGE
        chunk = files[start : start + _FILES_PER_PAGE]

        selected_cnt = sum(1 for f in files if int(f.get("priority", 1)) > 0)
        lines = [
            self._tr.t(
                "files_header",
                name=t_name,
                selected=selected_cnt,
                total=total,
                page=fpage + 1,
                pages=total_pages,
            ),
            "",
        ]

        rows: List[List[Dict[str, str]]] = []
        for pos, f in enumerate(chunk, start=start):
            file_idx = int(f.get("index", pos))
            prio = int(f.get("priority", 1))
            icon = "✅" if prio > 0 else "⬜"
            raw_name = str(f.get("name", f"File {file_idx}")).split("/")[-1]
            size_lbl = format_size(f.get("size", 0))
            pct = float(f.get("progress", 0.0)) * 100.0

            lines.append(
                f"{icon} <b>{pos + 1}.</b> {escape_html(truncate(raw_name, 40))} "
                f"({size_lbl} • {pct:.0f}%)"
            )
            if is_admin:
                rows.append(
                    [
                        {
                            "text": f"{icon} {pos + 1}. {truncate(raw_name, 26)} ({size_lbl})",
                            "callback_data": f"ft:{short_hash}:{flt}:{page}:{fpage}:{file_idx}",
                        }
                    ]
                )

        if is_admin:
            rows.append(
                [
                    {
                        "text": f"✅ {self._tr.t('btn_select_all')}",
                        "callback_data": f"fa:1:{short_hash}:{flt}:{page}:{fpage}",
                    },
                    {
                        "text": f"⬜ {self._tr.t('btn_deselect_all')}",
                        "callback_data": f"fa:0:{short_hash}:{flt}:{page}:{fpage}",
                    },
                ]
            )

        nav_row: List[Dict[str, str]] = []
        if fpage > 0:
            nav_row.append(
                {
                    "text": f"⬅️ {self._tr.t('btn_prev')}",
                    "callback_data": f"fl:{short_hash}:{flt}:{page}:{fpage - 1}",
                }
            )
        nav_row.append(
            {
                "text": f"⬅️ {self._tr.t('btn_back')}",
                "callback_data": f"dt:{short_hash}:{flt}:{page}",
            }
        )
        if fpage + 1 < total_pages:
            nav_row.append(
                {
                    "text": f"{self._tr.t('btn_next')} ➡️",
                    "callback_data": f"fl:{short_hash}:{flt}:{page}:{fpage + 1}",
                }
            )
        rows.append(nav_row)

        self._tg.edit_message_text(
            chat_id, msg_id, "\n".join(lines), reply_markup={"inline_keyboard": rows}
        )
        self._tg.answer_callback_query(cb_id, text=toast)

    def _toggle_file_priority(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        short_hash: str,
        flt: str,
        page: int,
        fpage: int,
        file_idx: int,
    ) -> None:
        torrent = self._find_by_short_hash(short_hash)
        if not torrent:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("torrent_not_found"), show_alert=True
            )
            return

        full_hash = str(torrent["hash"])
        files = self._qbt.get_torrent_files(full_hash)
        current_prio = 1
        for pos, f in enumerate(files):
            if int(f.get("index", pos)) == file_idx:
                current_prio = int(f.get("priority", 1))
                break

        new_prio = 0 if current_prio > 0 else 1
        self._qbt.set_file_priority(full_hash, [file_idx], new_prio)
        toast = self._tr.t("file_prio_on" if new_prio > 0 else "file_prio_off")
        self._show_files(
            cb_id, chat_id, msg_id, short_hash, flt, page, fpage, is_admin=True, toast=toast
        )

    def _set_all_files_priority(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        short_hash: str,
        flt: str,
        page: int,
        fpage: int,
        enable: bool,
    ) -> None:
        torrent = self._find_by_short_hash(short_hash)
        if not torrent:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("torrent_not_found"), show_alert=True
            )
            return

        full_hash = str(torrent["hash"])
        files = self._qbt.get_torrent_files(full_hash)
        file_ids = [int(f.get("index", pos)) for pos, f in enumerate(files)]
        if file_ids:
            self._qbt.set_file_priority(full_hash, file_ids, 1 if enable else 0)
        toast = self._tr.t("files_all_on" if enable else "files_all_off")
        self._show_files(
            cb_id, chat_id, msg_id, short_hash, flt, page, fpage, is_admin=True, toast=toast
        )

    # =========================================================================
    # IZLEYICI DETAYLARI (Passkey Korumali - v1.5)
    # =========================================================================

    def _show_trackers(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        short_hash: str,
        flt: str,
        page: int,
        is_admin: bool,
    ) -> None:
        torrent = self._find_by_short_hash(short_hash)
        if not torrent:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("torrent_not_found"), show_alert=True
            )
            return

        trackers = self._qbt.get_torrent_trackers(str(torrent["hash"]))
        t_name = escape_html(truncate(str(torrent.get("name", "Unknown")), 44))

        lines = [self._tr.t("trackers_header", name=t_name, count=len(trackers)), ""]
        if not trackers:
            lines.append(self._tr.t("trackers_empty"))
        else:
            for trk in trackers[:15]:
                host = safe_tracker_host(str(trk.get("url", "")))
                status_lbl = format_tracker_status(int(trk.get("status", 0)))
                seeds = int(trk.get("num_seeds", -1))
                peers = int(trk.get("num_peers", -1))
                seed_str = str(seeds) if seeds >= 0 else "—"
                peer_str = str(peers) if peers >= 0 else "—"
                err_msg = str(trk.get("msg", "")).strip()

                lines.append(f"• <code>{host}</code>")
                lines.append(f"  {status_lbl} | S: <b>{seed_str}</b> • P: <b>{peer_str}</b>")
                if err_msg:
                    lines.append(f"  <i>{escape_html(truncate(err_msg, 60))}</i>")

        rows: List[List[Dict[str, str]]] = []
        if is_admin:
            rows.append(
                [
                    {
                        "text": f"📡 {self._tr.t('btn_reannounce')}",
                        "callback_data": f"ac:ra:{short_hash}:{flt}:{page}",
                    }
                ]
            )
        rows.append(
            [
                {
                    "text": f"⬅️ {self._tr.t('btn_back')}",
                    "callback_data": f"dt:{short_hash}:{flt}:{page}",
                },
                {
                    "text": f"🔄 {self._tr.t('btn_refresh')}",
                    "callback_data": f"tr:{short_hash}:{flt}:{page}",
                },
            ]
        )

        self._tg.edit_message_text(
            chat_id, msg_id, "\n".join(lines), reply_markup={"inline_keyboard": rows}
        )
        self._tg.answer_callback_query(cb_id)

    # =========================================================================
    # TORRENT BAZLI HIZ LIMITI (v1.5)
    # =========================================================================

    def _show_torrent_limits(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        short_hash: str,
        flt: str,
        page: int,
        toast: Optional[str] = None,
    ) -> None:
        torrent = self._find_by_short_hash(short_hash)
        if not torrent:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("torrent_not_found"), show_alert=True
            )
            return

        unlim = self._tr.t("unlimited")
        text = self._tr.t(
            "tlimit_msg",
            name=escape_html(truncate(str(torrent.get("name", "Unknown")), 48)),
            dl_limit=format_limit(torrent.get("dl_limit", 0), unlim),
            ul_limit=format_limit(torrent.get("up_limit", 0), unlim),
        )

        dl_row = [
            {
                "text": f"⬇️ {self._preset_label(kb)}",
                "callback_data": f"ts:d:{kb}:{short_hash}:{flt}:{page}",
            }
            for kb in _TORRENT_PRESETS_KB
        ]
        ul_row = [
            {
                "text": f"⬆️ {self._preset_label(kb)}",
                "callback_data": f"ts:u:{kb}:{short_hash}:{flt}:{page}",
            }
            for kb in _TORRENT_PRESETS_KB
        ]
        rows = [
            dl_row,
            ul_row,
            [
                {
                    "text": f"⬅️ {self._tr.t('btn_back')}",
                    "callback_data": f"dt:{short_hash}:{flt}:{page}",
                }
            ],
        ]
        self._tg.edit_message_text(
            chat_id, msg_id, text, reply_markup={"inline_keyboard": rows}
        )
        self._tg.answer_callback_query(cb_id, text=toast)

    def _apply_torrent_limit(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        direction: str,
        limit_bytes: int,
        short_hash: str,
        flt: str,
        page: int,
    ) -> None:
        torrent = self._find_by_short_hash(short_hash)
        if not torrent:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("torrent_not_found"), show_alert=True
            )
            return

        full_hash = str(torrent["hash"])
        if direction == "d":
            self._qbt.set_torrent_download_limit(full_hash, limit_bytes)
        else:
            self._qbt.set_torrent_upload_limit(full_hash, limit_bytes)

        self._show_torrent_limits(
            cb_id,
            chat_id,
            msg_id,
            short_hash,
            flt,
            page,
            toast=self._tr.t("tlimit_updated"),
        )

    # =========================================================================
    # TORRENT AKSIYONLARI VE CIFT ONAYLI SILME
    # =========================================================================

    def _run_torrent_action(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        action: str,
        short_hash: str,
        flt: str,
        page: int,
    ) -> None:
        torrent = self._find_by_short_hash(short_hash)
        if not torrent:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("torrent_not_found"), show_alert=True
            )
            return

        full_hash = str(torrent["hash"])

        if action == "dl":
            text = self._tr.t(
                "delete_confirm_msg",
                name=escape_html(str(torrent.get("name", "Unknown"))),
            )
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": f"📄 {self._tr.t('btn_del_keep')}",
                            "callback_data": f"dc:k:{short_hash}:{flt}:{page}",
                        }
                    ],
                    [
                        {
                            "text": f"🔥 {self._tr.t('btn_del_files')}",
                            "callback_data": f"dc:f:{short_hash}:{flt}:{page}",
                        }
                    ],
                    [
                        {
                            "text": f"↩️ {self._tr.t('btn_cancel')}",
                            "callback_data": f"dt:{short_hash}:{flt}:{page}",
                        }
                    ],
                ]
            }
            self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
            self._tg.answer_callback_query(cb_id)
            return

        toast = ""
        if action == "ps":
            self._qbt.pause_torrents(full_hash)
            toast = self._tr.t("action_paused")
        elif action == "rs":
            self._qbt.resume_torrents(full_hash)
            toast = self._tr.t("action_resumed")
        elif action == "fs":
            self._qbt.force_start_torrents(full_hash, value=True)
            toast = self._tr.t("action_force_started")
        elif action == "rc":
            self._qbt.recheck_torrents(full_hash)
            toast = self._tr.t("action_rechecked")
        elif action == "ra":
            self._qbt.reannounce_torrents(full_hash)
            toast = self._tr.t("action_reannounced")

        self._show_detail(
            cb_id, chat_id, msg_id, short_hash, flt, page, is_admin=True, toast=toast
        )

    def _run_delete_confirmed(
        self,
        cb_id: str,
        chat_id: int,
        msg_id: int,
        mode: str,
        short_hash: str,
        flt: str,
        page: int,
    ) -> None:
        torrent = self._find_by_short_hash(short_hash)
        if not torrent:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("torrent_not_found"), show_alert=True
            )
            return

        delete_files = mode == "f"
        self._qbt.delete_torrents(str(torrent["hash"]), delete_files=delete_files)
        toast = (
            self._tr.t("deleted_files") if delete_files else self._tr.t("deleted_keep")
        )
        text, markup = self._render_list(flt, page)
        self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
        self._tg.answer_callback_query(cb_id, text=toast)

    # =========================================================================
    # SISTEM / DISK / S.M.A.R.T. VE RSS YONETIMI (v2.0)
    # =========================================================================

    def _send_sysinfo(self, chat_id: int) -> None:
        snap = collect_system_snapshot(
            disk_paths=self._disk_paths,
            smart_devices=self._smart_devices,
            preferred_save_path=self._default_save_path,
        )
        lines = [
            self._tr.t(
                "sysinfo_header",
                os_name=escape_html(snap["os_name"]),
                uptime=format_eta(snap["uptime_sec"]),
                load=escape_html(snap["load"]),
            ),
            "",
        ]

        thermals = snap.get("thermals") or []
        if thermals:
            t_parts = [
                f"{escape_html(z['name'])}: <b>{z['temp_c']:.1f}°C</b>"
                for z in thermals[:3]
            ]
            lines.append("🌡 Sistem Sıcaklığı: " + " | ".join(t_parts))

        if snap["mem_total"] > 0:
            mem_pct = snap["mem_used"] / snap["mem_total"] * 100.0
            lines.append(
                self._tr.t(
                    "sysinfo_mem",
                    used=format_size(snap["mem_used"]),
                    total=format_size(snap["mem_total"]),
                    pct=f"{mem_pct:.1f}",
                )
            )
        if snap["swap_total"] > 0:
            swap_pct = snap["swap_used"] / snap["swap_total"] * 100.0
            lines.append(
                self._tr.t(
                    "sysinfo_swap",
                    used=format_size(snap["swap_used"]),
                    total=format_size(snap["swap_total"]),
                    pct=f"{swap_pct:.1f}",
                )
            )
            for sw in snap.get("swaps") or []:
                lines.append(
                    f"   • <code>{escape_html(sw['name'])}</code>: "
                    f"{format_size(sw['used'])} / {format_size(sw['total'])} "
                    f"(<b>%{sw['used_pct']:.1f}</b>)"
                )
        if snap["bot_rss"] > 0:
            lines.append(
                self._tr.t("sysinfo_bot_rss", rss=format_size(snap["bot_rss"]))
            )

        disks = snap.get("disks") or []
        if disks:
            lines.append("")
            lines.append("💾 <b>Depolama Birimleri</b>")
            for d in disks:
                label = escape_html(str(d.get("label") or d["path"]))
                mnt = escape_html(truncate(str(d["path"]), 38))
                if not d.get("exists"):
                    lines.append(f"• <b>{label}</b> (<code>{mnt}</code>): —")
                    continue
                dev_str = escape_html(str(d.get("device") or "—"))
                fs_str = escape_html(str(d.get("fstype") or "—"))
                lines.append(f"\n📁 <b>{label}</b> — <code>{mnt}</code>")
                lines.append(
                    f"   • Aygıt: <code>{dev_str}</code> ({fs_str}) | "
                    f"Toplam: <b>{format_size(d['total'])}</b>"
                )
                lines.append(
                    f"   • Dolu: <b>{format_size(d['used'])}</b> (%{d['used_pct']:.1f}) | "
                    f"Boş: <b>{format_size(d['free'])}</b> (%{d['free_pct']:.1f})"
                )

        smart_list = snap.get("smart") or []
        if smart_list:
            lines.append("")
            lines.append("🩺 <b>Fiziksel Diskler &amp; S.M.A.R.T.</b>")
            for s in smart_list:
                is_hdd = s.get("media_type") == "HDD"
                icon = "💿" if is_hdd else "🔌"
                title_parts = [
                    p
                    for p in (s.get("vendor", "").strip(), s.get("model", "").strip())
                    if p and (not s.get("model") or p.lower() not in s["model"].lower() or p == s["model"].strip())
                ]
                model_lbl = " ".join(title_parts) if title_parts else "Bilinmeyen Disk"
                media_parts = []
                if s.get("media_type"):
                    media_parts.append(s["media_type"])
                if s.get("rpm"):
                    media_parts.append(f"{s['rpm']} RPM")
                media_str = f" ({' • '.join(media_parts)})" if media_parts else ""

                lines.append(
                    f"\n{icon} <code>{escape_html(s['device'])}</code> — "
                    f"<b>{escape_html(truncate(model_lbl, 34))}</b>{escape_html(media_str)}"
                )

                serial_str = escape_html(s["serial"]) if s.get("serial") else "—"
                fw_part = f" | FW: <code>{escape_html(s['firmware'])}</code>" if s.get("firmware") else ""
                lines.append(f"   • Seri No: <code>{serial_str}</code>{fw_part}")

                meta_bits: List[str] = []
                if s.get("usb_speed"):
                    meta_bits.append(f"Bağlantı: {escape_html(s['usb_speed'])}")
                if s.get("physical_bytes", 0) > 0:
                    meta_bits.append(f"Kapasite: <b>{format_size(s['physical_bytes'])}</b>")
                if meta_bits:
                    lines.append("   • " + " | ".join(meta_bits))

                if s.get("smart_supported"):
                    health = (
                        "🟢 OK"
                        if s.get("passed") is True
                        else ("🔴 FAIL" if s.get("passed") is False else "⚪ —")
                    )
                    temp_str = f"{s['temp_c']}°C" if s.get("temp_c") is not None else "—"
                    lines.append(f"   • Sağlık: {health} | 🌡 Sıcaklık: <b>{temp_str}</b>")

                    usage_bits: List[str] = []
                    if s.get("power_on_hours") is not None:
                        hrs = int(s["power_on_hours"])
                        days = hrs // 24
                        usage_bits.append(f"Çalışma: <b>{hrs:,}</b> sa (~{days} gün)")
                    if s.get("power_cycles") is not None:
                        usage_bits.append(f"Açılış: <b>{int(s['power_cycles']):,}</b>")
                    if usage_bits:
                        lines.append("   • " + " | ".join(usage_bits))

                    sec_bits: List[str] = []
                    if s.get("reallocated") is not None:
                        sec_bits.append(f"Yeniden: {s['reallocated']}")
                    if s.get("pending") is not None:
                        sec_bits.append(f"Bekleyen: {s['pending']}")
                    if s.get("uncorrectable") is not None:
                        sec_bits.append(f"Bozuk: {s['uncorrectable']}")
                    if s.get("crc_errors") is not None:
                        sec_bits.append(f"CRC: {s['crc_errors']}")
                    if sec_bits:
                        lines.append("   • Sektör: " + " | ".join(sec_bits))
                else:
                    lines.append("   • S.M.A.R.T.: Desteklemiyor | 🌡 Sıcaklık: —")

        message = "\n".join(lines)
        if len(message) > 4000:
            message = message[:3990] + "\n…"
        self._tg.send_message(chat_id, message)

    def _handle_rss_command(self, chat_id: int, arg: str) -> None:
        if not arg:
            items = self._qbt.get_rss_items(with_data=False)
            if not items:
                self._tg.send_message(chat_id, self._tr.t("rss_empty"))
                return
            lines = [self._tr.t("rss_header", count=len(items)), ""]
            for name, info in list(items.items())[:15]:
                if isinstance(info, dict) and "url" in info:
                    lines.append(
                        f"• <b>{escape_html(truncate(str(name), 36))}</b>\n"
                        f"  <code>{escape_html(truncate(str(info['url']), 48))}</code>"
                    )
                else:
                    lines.append(f"📁 <b>{escape_html(truncate(str(name), 36))}</b>")
            self._tg.send_message(chat_id, "\n".join(lines))
            return

        parts = arg.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "add" and rest:
            add_parts = rest.split()
            url = add_parts[0]
            feed_name = add_parts[1] if len(add_parts) > 1 else ""
            if not (url.startswith("http://") or url.startswith("https://")):
                self._tg.send_message(chat_id, self._tr.t("rss_usage"))
                return
            self._qbt.add_rss_feed(url=url, path=feed_name)
            self._tg.send_message(
                chat_id,
                self._tr.t("rss_added", name=escape_html(feed_name or url)),
            )
        elif sub in ("del", "remove") and rest:
            self._qbt.remove_rss_item(rest)
            self._tg.send_message(
                chat_id, self._tr.t("rss_removed", name=escape_html(rest))
            )
        else:
            self._tg.send_message(chat_id, self._tr.t("rss_usage"))

    # =========================================================================
    # ZAMANLANMIS HIZ PROFILLERI (v2.0)
    # =========================================================================

    def _render_schedule_panel(self) -> Tuple[str, Dict[str, Any]]:
        if self._scheduler is None:
            return self._tr.t("sched_unavailable"), {"inline_keyboard": []}

        info = self._scheduler.get_summary()
        unlim = self._tr.t("unlimited")
        state_lbl = self._tr.t("on_label" if info["enabled"] else "off_label")
        cur_lbl = self._tr.t(
            "sched_profile_night"
            if info["current_profile"] == "night"
            else "sched_profile_day"
        )

        text = self._tr.t(
            "sched_msg",
            state=state_lbl,
            current=cur_lbl,
            night_start=info["night_start"],
            night_end=info["night_end"],
            night_dl=format_limit(info["night_dl_bytes"], unlim),
            night_ul=format_limit(info["night_ul_bytes"], unlim),
            day_dl=format_limit(info["day_dl_bytes"], unlim),
            day_ul=format_limit(info["day_ul_bytes"], unlim),
        )

        toggle_icon = "⏸" if info["enabled"] else "▶️"
        rows = [
            [
                {
                    "text": f"{toggle_icon} {self._tr.t('btn_sched_toggle')}: {state_lbl}",
                    "callback_data": "sc:tg",
                }
            ],
            [
                {
                    "text": f"⚡ {self._tr.t('btn_sched_apply')}",
                    "callback_data": "sc:ap",
                },
                {
                    "text": f"🔄 {self._tr.t('btn_refresh')}",
                    "callback_data": "sc:rf",
                },
            ],
        ]
        return text, {"inline_keyboard": rows}

    def _handle_schedule_command(self, chat_id: int, arg: str) -> None:
        if self._scheduler is None:
            self._tg.send_message(chat_id, self._tr.t("sched_unavailable"))
            return

        sub = arg.strip().lower()
        if sub in ("on", "enable", "1"):
            self._scheduler.set_enabled(True)
        elif sub in ("off", "disable", "0"):
            self._scheduler.set_enabled(False)
        elif sub == "apply":
            self._scheduler.apply_now(notify=False)

        text, markup = self._render_schedule_panel()
        self._tg.send_message(chat_id, text, reply_markup=markup)

    def _handle_schedule_callback(
        self, cb_id: str, chat_id: int, msg_id: int, action: str
    ) -> None:
        if self._scheduler is None:
            self._tg.answer_callback_query(
                cb_id, text=self._tr.t("sched_unavailable"), show_alert=True
            )
            return

        toast = self._tr.t("callback_refreshed")
        if action == "tg":
            new_state = not self._scheduler.enabled
            self._scheduler.set_enabled(new_state)
            toast = self._tr.t(
                "sched_toggled_on" if new_state else "sched_toggled_off"
            )
        elif action == "ap":
            self._scheduler.apply_now(notify=False)
            toast = self._tr.t("sched_applied")

        text, markup = self._render_schedule_panel()
        self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
        self._tg.answer_callback_query(cb_id, text=toast)