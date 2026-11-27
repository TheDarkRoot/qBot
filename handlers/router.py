from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from api.qbittorrent import QBittorrentClient, QBittorrentError
from core.auth import AuthManager, Role
from core.formatters import (
    format_eta,
    format_size,
    format_speed,
    format_state,
    is_paused_state,
    progress_bar,
    safe_category,
    truncate,
)
from core.i18n import Translator
from telegram.client import TelegramClient, TelegramError, escape_html

_MAGNET_RE = re.compile(r"magnet:\?xt=urn:[^\s]+", re.IGNORECASE)
_TORRENT_URL_RE = re.compile(r"https?://[^\s]+\.torrent(?:\?[^\s]*)?", re.IGNORECASE)

# Kisa callback kodlari -> (qBittorrent API filtresi, dil anahtari)
_FILTERS: Dict[str, Tuple[Optional[str], str]] = {
    "all": (None, "filter_all"),
    "dl": ("downloading", "filter_dl"),
    "up": ("seeding", "filter_up"),
    "cp": ("completed", "filter_cp"),
    "ps": ("paused", "filter_ps"),
    "er": ("errored", "filter_er"),
}


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
    ) -> None:
        self._tg = tg
        self._qbt = qbt
        self._auth = auth
        self._tr = translator
        self._default_save_path = default_save_path
        self._per_page = max(1, min(15, int(items_per_page)))
        self._logger = logging.getLogger("qbot.router")

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

        # 1. .torrent belge kontrolu
        document = msg.get("document")
        if isinstance(document, dict):
            self._handle_torrent_document(chat_id, document)
            return

        text = (msg.get("text") or "").strip()
        if not text:
            return

        # 2. Komut kontrolu
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].split("@")[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            self._dispatch_command(chat_id, user_id, role, cmd, arg)
            return

        # 3. Duz mesaj icinde magnet veya .torrent URL algilama
        magnets = _MAGNET_RE.findall(text)
        torrent_urls = _TORRENT_URL_RE.findall(text)
        links = magnets + [u for u in torrent_urls if u not in magnets]
        if links:
            self._add_links(chat_id, links)

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
                self._add_links(chat_id, [arg])
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

    def _add_links(self, chat_id: int, links: List[str]) -> None:
        try:
            ok = self._qbt.add_magnet(
                urls="\n".join(links),
                save_path=self._default_save_path,
            )
            if ok:
                self._tg.send_message(
                    chat_id, self._tr.t("added_magnet", count=len(links))
                )
            else:
                self._tg.send_message(chat_id, self._tr.t("add_failed"))
        except QBittorrentError as exc:
            self._tg.send_message(
                chat_id, self._tr.t("api_error", error=escape_html(str(exc)))
            )

    def _handle_torrent_document(self, chat_id: int, document: Dict[str, Any]) -> None:
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
            ok = self._qbt.add_torrent_file(
                filename=filename,
                content=content,
                save_path=self._default_save_path,
            )
            if ok:
                self._tg.send_message(
                    chat_id,
                    self._tr.t("added_file", filename=escape_html(filename)),
                )
            else:
                self._tg.send_message(chat_id, self._tr.t("add_failed"))
        except (TelegramError, QBittorrentError) as exc:
            self._tg.send_message(
                chat_id, self._tr.t("api_error", error=escape_html(str(exc)))
            )

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
            if kind == "st":
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
                else:
                    self._qbt.resume_torrents("all")
                    notice = self._tr.t("action_resumed_all")
                text, markup = self._render_status(is_admin=True)
                self._tg.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
                self._tg.answer_callback_query(cb_id, text=notice)

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
    # GORUNUM OLUSTURUCULAR (Durum, Liste, Detay, Silme Onayi)
    # =========================================================================

    def _render_status(self, is_admin: bool) -> Tuple[str, Dict[str, Any]]:
        app_ver = self._qbt.get_app_version()
        transfer = self._qbt.get_transfer_info()
        turtle_on = self._qbt.get_speed_limits_mode()
        torrents = self._qbt.get_torrents()

        dl_cnt = sum(1 for t in torrents if t.get("state") in ("downloading", "forcedDL", "metaDL", "stalledDL"))
        up_cnt = sum(1 for t in torrents if t.get("state") in ("uploading", "forcedUP", "stalledUP"))
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
            rows.append(
                [
                    {"text": f"⏸ {self._tr.t('btn_pause_all')}", "callback_data": "gl:pa"},
                    {"text": f"▶️ {self._tr.t('btn_resume_all')}", "callback_data": "gl:ra"},
                ]
            )
        rows.append([{"text": f"🔄 {self._tr.t('btn_refresh')}", "callback_data": "st:ref"}])
        return text, {"inline_keyboard": rows}

    def _send_list(self, chat_id: int, flt: str, page: int) -> None:
        text, markup = self._render_list(flt, page)
        self._tg.send_message(chat_id, text, reply_markup=markup)

    def _render_list(self, flt: str, page: int) -> Tuple[str, Dict[str, Any]]:
        api_filter, title_key = _FILTERS.get(flt, (None, "filter_all"))
        title = self._tr.t(title_key)
        torrents = self._qbt.get_torrents(filter_state=api_filter)
        total = len(torrents)

        rows: List[List[Dict[str, str]]] = []
        # Ust hizli filtre seridi
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
            btn_label = f"{idx}. {truncate(str(t.get('name', '')), 26)} ({pct:.0f}%)"
            rows.append(
                [{"text": btn_label, "callback_data": f"dt:{short_hash}:{flt}:{page}"}]
            )

        # Sayfalama ve yenileme seridi
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

        rows: List[List[Dict[str, str]]] = []
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
                        "text": f"📡 {self._tr.t('btn_reannounce')}",
                        "callback_data": f"ac:ra:{short_hash}:{flt}:{page}",
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
            # Cift onayli silme ekrani
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