from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class TelegramError(Exception):
    """Base exception for Telegram Bot API errors."""


class TelegramAPIError(TelegramError):
    """Raised when Telegram returns an API-level error (ok=false)."""

    def __init__(
        self,
        description: str,
        error_code: int = 0,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(f"Telegram API error {error_code}: {description}")
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after


class TelegramNetworkError(TelegramError):
    """Raised on network timeouts, DNS failures, or connection drops."""


def escape_html(text: Any) -> str:
    """Escape HTML special characters safely without extra stdlib imports."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class TelegramClient:
    """Zero-dependency Telegram Bot API client using Python stdlib only."""

    _API_BASE = "https://api.telegram.org"

    def __init__(self, bot_token: str, request_timeout: int = 20) -> None:
        self._token = bot_token.strip()
        self._request_timeout = request_timeout
        self._logger = logging.getLogger("qbot.telegram")
        self._ssl_context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_context)
        )

    def _sanitize(self, text: str) -> str:
        if self._token and self._token in text:
            return text.replace(self._token, "[REDACTED]")
        return text

    def _call(
        self,
        method: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Any:
        url = f"{self._API_BASE}/bot{self._token}/{method}"
        clean_payload = {
            k: v for k, v in (payload or {}).items() if v is not None
        }
        body = json.dumps(clean_payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            url=url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "qbot/2.0",
            },
            method="POST",
        )

        effective_timeout = timeout if timeout is not None else self._request_timeout

        try:
            with self._opener.open(req, timeout=effective_timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            err_body = b""
            try:
                err_body = exc.read()
            except OSError:
                pass
            description = self._sanitize(str(exc.reason))
            retry_after: Optional[int] = None
            if err_body:
                try:
                    parsed_err = json.loads(err_body.decode("utf-8", errors="replace"))
                    description = self._sanitize(
                        str(parsed_err.get("description", description))
                    )
                    params = parsed_err.get("parameters")
                    if isinstance(params, dict) and "retry_after" in params:
                        retry_after = int(params["retry_after"])
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
            raise TelegramAPIError(
                description=description,
                error_code=exc.code,
                retry_after=retry_after,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramNetworkError(
                f"Telegram network error on {method}: {self._sanitize(str(exc))}"
            ) from exc

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TelegramError(f"Invalid JSON from Telegram on {method}") from exc

        if not parsed.get("ok"):
            params = parsed.get("parameters") or {}
            retry_after = (
                int(params["retry_after"]) if "retry_after" in params else None
            )
            raise TelegramAPIError(
                description=self._sanitize(str(parsed.get("description", "Unknown error"))),
                error_code=int(parsed.get("error_code", 0)),
                retry_after=retry_after,
            )

        return parsed.get("result")

    # ---- Temel Bot ve Polling Metotlari ----

    def get_me(self) -> Dict[str, Any]:
        return self._call("getMe")

    def set_my_commands(self, commands: List[Dict[str, str]]) -> bool:
        return bool(self._call("setMyCommands", {"commands": commands}))

    def get_updates(
        self,
        offset: Optional[int] = None,
        timeout: int = 30,
        allowed_updates: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": allowed_updates or ["message", "callback_query"],
        }
        # HTTP zaman asimi Telegram long-polling suresinden mutlaka uzun olmali
        http_timeout = timeout + 15
        result = self._call("getUpdates", payload=payload, timeout=http_timeout)
        return result if isinstance(result, list) else []

    # ---- Mesaj ve Buton Metotlari ----

    def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_web_page_preview: bool = True,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
            "disable_web_page_preview": disable_web_page_preview,
            "reply_to_message_id": reply_to_message_id,
        }
        return self._call("sendMessage", payload=payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: Optional[str] = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_web_page_preview: bool = True,
    ) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
            "disable_web_page_preview": disable_web_page_preview,
        }
        try:
            return self._call("editMessageText", payload=payload)
        except TelegramAPIError as exc:
            if "message is not modified" in exc.description.lower():
                return None
            raise

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        try:
            return bool(
                self._call(
                    "deleteMessage",
                    payload={"chat_id": chat_id, "message_id": message_id},
                )
            )
        except TelegramAPIError:
            return False

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        payload: Dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        try:
            return bool(self._call("answerCallbackQuery", payload=payload))
        except TelegramAPIError:
            return False

    # ---- .torrent Dosyasi Indirme (RAM Korumali) ----

    def get_file(self, file_id: str) -> Dict[str, Any]:
        return self._call("getFile", payload={"file_id": file_id})

    def download_file(
        self, file_path: str, max_size_bytes: int = 2_097_152
    ) -> bytes:
        """Download a file from Telegram servers into RAM with a strict size cap."""
        clean_path = file_path.lstrip("/")
        url = f"{self._API_BASE}/file/bot{self._token}/{clean_path}"
        req = urllib.request.Request(url=url, headers={"User-Agent": "qbot/2.0"})

        try:
            with self._opener.open(req, timeout=self._request_timeout) as resp:
                chunks: List[bytes] = []
                total = 0
                while True:
                    chunk = resp.read(16_384)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_size_bytes:
                        raise TelegramError(
                            f"File exceeds maximum allowed size ({max_size_bytes} bytes)."
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except urllib.error.HTTPError as exc:
            raise TelegramAPIError(
                description=self._sanitize(str(exc.reason)),
                error_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramNetworkError(
                f"File download failed: {self._sanitize(str(exc))}"
            ) from exc