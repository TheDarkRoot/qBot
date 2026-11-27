from __future__ import annotations

import time
from enum import Enum
from typing import Dict, Iterable, Optional


class Role(Enum):
    ADMIN = "admin"
    USER = "user"
    UNAUTHORIZED = "unauthorized"


class AuthManager:
    """Role-based access control with anti-spam throttling for unauthorized IDs."""

    _MAX_TRACKED_UNAUTH = 500

    def __init__(
        self,
        admin_ids: Iterable[int],
        user_ids: Iterable[int] = (),
        unauth_cooldown_sec: int = 60,
    ) -> None:
        self._admins = frozenset(int(uid) for uid in admin_ids)
        self._users = frozenset(int(uid) for uid in user_ids) - self._admins
        self._cooldown = max(5, int(unauth_cooldown_sec))
        self._unauth_seen: Dict[int, float] = {}

    def get_role(self, user_id: Optional[int]) -> Role:
        if user_id is None:
            return Role.UNAUTHORIZED
        if user_id in self._admins:
            return Role.ADMIN
        if user_id in self._users:
            return Role.USER
        return Role.UNAUTHORIZED

    def is_authorized(self, user_id: Optional[int]) -> bool:
        return self.get_role(user_id) is not Role.UNAUTHORIZED

    def is_admin(self, user_id: Optional[int]) -> bool:
        return self.get_role(user_id) is Role.ADMIN

    def should_notify_unauthorized(self, user_id: int) -> bool:
        """Throttle unauthorized warnings/replies to prevent log and API flooding."""
        now = time.monotonic()
        # Router RAM korumasi: yetkisiz kayit tablosu asiri buyurse temizle
        if len(self._unauth_seen) >= self._MAX_TRACKED_UNAUTH:
            expired = [
                uid
                for uid, ts in self._unauth_seen.items()
                if (now - ts) >= self._cooldown
            ]
            for uid in expired:
                del self._unauth_seen[uid]
            if len(self._unauth_seen) >= self._MAX_TRACKED_UNAUTH:
                self._unauth_seen.clear()

        last_seen = self._unauth_seen.get(user_id)
        if last_seen is not None and (now - last_seen) < self._cooldown:
            return False

        self._unauth_seen[user_id] = now
        return True