from __future__ import annotations

import http.cookiejar
import json
import logging
import mimetypes
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple


class QBittorrentError(Exception):
    """Base exception for qBittorrent API errors."""


class QBittorrentAuthError(QBittorrentError):
    """Raised when authentication fails or IP is temporarily banned."""


class QBittorrentConnectionError(QBittorrentError):
    """Raised when qBittorrent server cannot be reached."""


def _parse_version(version_str: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for token in version_str.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits:
            parts.append(int(digits))
        else:
            break
    return tuple(parts) if parts else (0, 0, 0)


def _encode_multipart_formdata(
    fields: Dict[str, str],
    files: List[Tuple[str, str, bytes]],
) -> Tuple[bytes, str]:
    """Build a multipart/form-data payload using Python standard library only."""
    boundary = f"----qbot{uuid.uuid4().hex}"
    chunks: List[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(f"{value}\r\n".encode("utf-8"))

    for field_name, filename, content in files:
        # Header injection onlemi: tehlikeli karakterleri temizle
        safe_name = filename.replace('"', "_").replace("\r", "").replace("\n", "")
        content_type = (
            mimetypes.guess_type(safe_name)[0] or "application/x-bittorrent"
        )
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{safe_name}"\r\n'
            ).encode("utf-8")
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        chunks.append(content)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    content_header = f"multipart/form-data; boundary={boundary}"
    return body, content_header


def _join_hashes(hashes: str | Iterable[str]) -> str:
    if isinstance(hashes, str):
        return hashes
    return "|".join(hashes)


class QBittorrentClient:
    """Zero-dependency, thread-safe qBittorrent WebUI API v2 client."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        timeout: int = 15,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._logger = logging.getLogger("qbot.api")

        self._lock = threading.RLock()
        self._cookie_jar = http.cookiejar.CookieJar()
        self._webapi_version: Optional[Tuple[int, ...]] = None

        if verify_ssl:
            ssl_context = ssl.create_default_context()
        else:
            ssl_context = ssl._create_unverified_context()

        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
            urllib.request.HTTPSHandler(context=ssl_context),
        )

    @property
    def is_v5(self) -> bool:
        """qBittorrent v5.0+ uses WebAPI >= 2.11.0 (stop/start instead of pause/resume)."""
        if self._webapi_version is None:
            try:
                ver_str = self.get_webapi_version()
                self._webapi_version = _parse_version(ver_str)
            except QBittorrentError:
                self._webapi_version = (2, 0, 0)
        return self._webapi_version >= (2, 11, 0)

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[List[Tuple[str, str, bytes]]] = None,
        allow_relogin: bool = True,
    ) -> bytes:
        url = f"{self._base_url}{endpoint}"
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}
            if clean_params:
                url = f"{url}?{urllib.parse.urlencode(clean_params)}"

        headers = {
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/",
            "User-Agent": "qbot/2.0",
        }
        body: Optional[bytes] = None

        if method.upper() == "POST":
            if files:
                str_fields = {
                    k: str(v) for k, v in (data or {}).items() if v is not None
                }
                body, content_type = _encode_multipart_formdata(str_fields, files)
                headers["Content-Type"] = content_type
            else:
                clean_data = {
                    k: v for k, v in (data or {}).items() if v is not None
                }
                body = urllib.parse.urlencode(clean_data).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(
            url=url,
            data=body,
            headers=headers,
            method=method.upper(),
        )

        with self._lock:
            try:
                with self._opener.open(req, timeout=self._timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 403 and allow_relogin and endpoint != "/api/v2/auth/login":
                    self._logger.info("qBittorrent session expired (403); re-authenticating...")
                    self.login()
                    return self._request(
                        method=method,
                        endpoint=endpoint,
                        params=params,
                        data=data,
                        files=files,
                        allow_relogin=False,
                    )
                if exc.code == 403:
                    raise QBittorrentAuthError(
                        "qBittorrent authentication failed or IP is banned (HTTP 403)."
                    ) from exc
                raise QBittorrentError(
                    f"qBittorrent API error {exc.code} on {endpoint}: {exc.reason}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise QBittorrentConnectionError(
                    f"Cannot reach qBittorrent at {self._base_url}: {exc}"
                ) from exc

    def _get_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        raw = self._request("GET", endpoint, params=params)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise QBittorrentError(f"Invalid JSON response from {endpoint}") from exc

    def _get_text(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> str:
        raw = self._request("GET", endpoint, params=params)
        return raw.decode("utf-8", errors="replace").strip()

    # ---- Kimlik Dogrulama ve Uygulama Bilgisi ----

    def login(self) -> None:
        with self._lock:
            self._cookie_jar.clear()
            raw = self._request(
                "POST",
                "/api/v2/auth/login",
                data={"username": self._username, "password": self._password},
                allow_relogin=False,
            )
            result = raw.decode("utf-8", errors="replace").strip()
            if result != "Ok.":
                raise QBittorrentAuthError(
                    f"Login rejected by qBittorrent (response: {result or 'empty'})."
                )
            self._logger.debug("Successfully authenticated with qBittorrent.")
            try:
                ver_str = self.get_webapi_version()
                self._webapi_version = _parse_version(ver_str)
            except QBittorrentError:
                self._webapi_version = (2, 0, 0)

    def get_app_version(self) -> str:
        return self._get_text("/api/v2/app/version")

    def get_webapi_version(self) -> str:
        return self._get_text("/api/v2/app/webapiVersion")

    def get_preferences(self) -> Dict[str, Any]:
        return self._get_json("/api/v2/app/preferences")

    # ---- Transfer ve Hiz Kontrolleri ----

    def get_transfer_info(self) -> Dict[str, Any]:
        return self._get_json("/api/v2/transfer/info")

    def get_speed_limits_mode(self) -> bool:
        """Returns True if alternative speed limits (Turtle mode) are active."""
        return self._get_text("/api/v2/transfer/speedLimitsMode") == "1"

    def toggle_speed_limits_mode(self) -> None:
        self._request("POST", "/api/v2/transfer/toggleSpeedLimitsMode")

    def set_global_download_limit(self, limit_bytes_per_sec: int) -> None:
        self._request(
            "POST",
            "/api/v2/transfer/setDownloadLimit",
            data={"limit": max(0, int(limit_bytes_per_sec))},
        )

    def set_global_upload_limit(self, limit_bytes_per_sec: int) -> None:
        self._request(
            "POST",
            "/api/v2/transfer/setUploadLimit",
            data={"limit": max(0, int(limit_bytes_per_sec))},
        )

    # ---- Torrent Listeleme ve Detay ----

    def get_torrents(
        self,
        filter_state: Optional[str] = None,
        category: Optional[str] = None,
        sort: str = "added_on",
        reverse: bool = True,
        hashes: Optional[str | Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        if filter_state == "paused" and self.is_v5:
            filter_state = "stopped"
        elif filter_state == "stopped" and not self.is_v5:
            filter_state = "paused"

        params: Dict[str, Any] = {
            "filter": filter_state,
            "category": category,
            "sort": sort,
            "reverse": "true" if reverse else "false",
        }
        if hashes is not None:
            params["hashes"] = _join_hashes(hashes)

        return self._get_json("/api/v2/torrents/info", params=params) or []

    def get_torrent_properties(self, torrent_hash: str) -> Dict[str, Any]:
        return self._get_json(
            "/api/v2/torrents/properties", params={"hash": torrent_hash}
        )

    def get_torrent_trackers(self, torrent_hash: str) -> List[Dict[str, Any]]:
        return (
            self._get_json("/api/v2/torrents/trackers", params={"hash": torrent_hash})
            or []
        )

    def get_torrent_files(self, torrent_hash: str) -> List[Dict[str, Any]]:
        return (
            self._get_json("/api/v2/torrents/files", params={"hash": torrent_hash}) or []
        )

    def get_categories(self) -> Dict[str, Any]:
        return self._get_json("/api/v2/torrents/categories") or {}

    # ---- Torrent Ekleme (Magnet / URL / .torrent Dosyasi) ----

    def _build_add_fields(
        self,
        save_path: str = "",
        category: str = "",
        paused: bool = False,
        sequential: bool = False,
        first_last_piece_prio: bool = False,
    ) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        if save_path:
            fields["savepath"] = save_path
        if category:
            fields["category"] = category
        if paused:
            fields["stopped" if self.is_v5 else "paused"] = "true"
        if sequential:
            fields["sequentialDownload"] = "true"
        if first_last_piece_prio:
            fields["firstLastPiecePrio"] = "true"
        return fields

    def add_magnet(
        self,
        urls: str,
        save_path: str = "",
        category: str = "",
        paused: bool = False,
        sequential: bool = False,
        first_last_piece_prio: bool = False,
    ) -> bool:
        fields = self._build_add_fields(
            save_path=save_path,
            category=category,
            paused=paused,
            sequential=sequential,
            first_last_piece_prio=first_last_piece_prio,
        )
        fields["urls"] = urls
        raw = self._request("POST", "/api/v2/torrents/add", data=fields)
        return raw.decode("utf-8", errors="replace").strip() == "Ok."

    def add_torrent_file(
        self,
        filename: str,
        content: bytes,
        save_path: str = "",
        category: str = "",
        paused: bool = False,
        sequential: bool = False,
        first_last_piece_prio: bool = False,
    ) -> bool:
        fields = self._build_add_fields(
            save_path=save_path,
            category=category,
            paused=paused,
            sequential=sequential,
            first_last_piece_prio=first_last_piece_prio,
        )
        raw = self._request(
            "POST",
            "/api/v2/torrents/add",
            data=fields,
            files=[("torrents", filename, content)],
        )
        return raw.decode("utf-8", errors="replace").strip() == "Ok."

    # ---- Torrent Kontrol Aksiyonlari ----

    def pause_torrents(self, hashes: str | Iterable[str]) -> None:
        endpoint = "/api/v2/torrents/stop" if self.is_v5 else "/api/v2/torrents/pause"
        self._request("POST", endpoint, data={"hashes": _join_hashes(hashes)})

    def resume_torrents(self, hashes: str | Iterable[str]) -> None:
        endpoint = "/api/v2/torrents/start" if self.is_v5 else "/api/v2/torrents/resume"
        self._request("POST", endpoint, data={"hashes": _join_hashes(hashes)})

    def delete_torrents(
        self, hashes: str | Iterable[str], delete_files: bool = False
    ) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": _join_hashes(hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
        )

    def recheck_torrents(self, hashes: str | Iterable[str]) -> None:
        self._request(
            "POST", "/api/v2/torrents/recheck", data={"hashes": _join_hashes(hashes)}
        )

    def reannounce_torrents(self, hashes: str | Iterable[str]) -> None:
        self._request(
            "POST", "/api/v2/torrents/reannounce", data={"hashes": _join_hashes(hashes)}
        )

    def force_start_torrents(
        self, hashes: str | Iterable[str], value: bool = True
    ) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/setForceStart",
            data={"hashes": _join_hashes(hashes), "value": "true" if value else "false"},
        )

    def set_file_priority(
        self, torrent_hash: str, file_ids: Iterable[int], priority: int
    ) -> None:
        """Priority: 0=Do not download, 1=Normal, 6=High, 7=Maximal."""
        joined_ids = "|".join(str(fid) for fid in file_ids)
        self._request(
            "POST",
            "/api/v2/torrents/filePrio",
            data={"hash": torrent_hash, "id": joined_ids, "priority": int(priority)},
        )

    def set_torrent_download_limit(
        self, hashes: str | Iterable[str], limit_bytes_per_sec: int
    ) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/setDownloadLimit",
            data={
                "hashes": _join_hashes(hashes),
                "limit": max(0, int(limit_bytes_per_sec)),
            },
        )

    def set_torrent_upload_limit(
        self, hashes: str | Iterable[str], limit_bytes_per_sec: int
    ) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/setUploadLimit",
            data={
                "hashes": _join_hashes(hashes),
                "limit": max(0, int(limit_bytes_per_sec)),
            },
        )

    # ---- RSS Yonetimi (v2.0 Hazirligi) ----

    def get_rss_items(self, with_data: bool = False) -> Dict[str, Any]:
        return (
            self._get_json(
                "/api/v2/rss/items",
                params={"withData": "true" if with_data else "false"},
            )
            or {}
        )

    def add_rss_feed(self, url: str, path: str = "") -> None:
        self._request("POST", "/api/v2/rss/addFeed", data={"url": url, "path": path})

    def remove_rss_item(self, path: str) -> None:
        self._request("POST", "/api/v2/rss/removeItem", data={"path": path})