"""Own login: bcrypt hashes in data/accounts.json plus a signed session cookie.

Replaces Caddy's basic auth (owner's request, 2026-10-03) so there is a real
login page and a logout. There is no server-side session state: the cookie holds
user and expiry, signed with a key derived from APP_SECRET_KEY and with the
account's current hash, so changing a password or removing an account ends its
sessions. Passwords are only set from the server (python -m app.passwd).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path

from Crypto.Protocol.KDF import bcrypt, bcrypt_check

from app.backend.users import valid_username

log = logging.getLogger(__name__)

COOKIE = "spot2deez_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD, MAX_PASSWORD_BYTES = 8, 72  # bcrypt ignores everything past 72 bytes


class Accounts:
    def __init__(self, path: Path, secret_key: str, cost: int = 12):
        self.path = Path(path)
        self._key = hashlib.sha256(b"spot2deez-session:" + secret_key.encode()).digest()
        self._cost = cost
        self._lock = threading.Lock()  # one bcrypt at a time: the server has a single core
        # Checked when the user does not exist, so the answer takes as long either way.
        self._dummy = bcrypt(b"no such user", cost)

    def users(self) -> list[str]:
        return sorted(self._load())

    def set_password(self, user: str, password: str) -> None:
        if not valid_username(user):
            raise ValueError("invalid user name (use a-z, 0-9, _ or -, up to 32)")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must have at least {MIN_PASSWORD} characters")
        if len(password.encode()) > MAX_PASSWORD_BYTES:
            raise ValueError(f"password must have at most {MAX_PASSWORD_BYTES} bytes")
        with self._lock:
            data = self._load()
            data[user] = bcrypt(password.encode(), self._cost).decode()
            self._save(data)

    def remove(self, user: str) -> bool:
        with self._lock:
            data = self._load()
            if data.pop(user, None) is None:
                return False
            self._save(data)
            return True

    def verify(self, user: str, password: str) -> bool:
        stored = self._load().get(user) if valid_username(user) else None
        # pycryptodome only parses "$2a$"; "$2b$"/"$2y$" (Caddy, htpasswd) are the same
        # algorithm for passwords up to 72 bytes.
        target = b"$2a$" + stored.encode()[4:] if stored else self._dummy
        with self._lock:
            try:
                bcrypt_check(password.encode()[:MAX_PASSWORD_BYTES], target)
            except ValueError:
                return False
        return stored is not None

    def new_session(self, user: str, now: float | None = None) -> str:
        stored = self._load().get(user)
        if not stored:
            raise ValueError("unknown user")
        expires = int((now if now is not None else time.time()) + SESSION_SECONDS)
        return f"{user}.{expires}.{self._mac(user, expires, stored)}"

    def session_user(self, token: str | None, now: float | None = None) -> str | None:
        """The user a session cookie belongs to, or None if it is forged, expired or revoked."""
        try:
            user, expires, mac = (token or "").split(".")
            expires = int(expires)
        except ValueError:
            return None
        stored = self._load().get(user) if valid_username(user) else None
        if not stored or expires < (now if now is not None else time.time()):
            return None
        return user if hmac.compare_digest(mac, self._mac(user, expires, stored)) else None

    def _mac(self, user: str, expires: int, stored: str) -> str:
        return hmac.new(self._key, f"{user}.{expires}.{stored}".encode(), hashlib.sha256).hexdigest()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("accounts file unreadable (%s); nobody can log in", type(e).__name__)
            return {}
        if not isinstance(data, dict):
            return {}
        return {u: h for u, h in data.items() if isinstance(h, str) and h.startswith("$2")}

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


class Throttle:
    """Refuses a client after too many wrong passwords (each check costs a bcrypt)."""

    def __init__(self, limit: int = 8, window: float = 300):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        with self._lock:
            for k in [k for k, v in self._fails.items() if v[-1] <= now - self.window]:
                del self._fails[k]
            recent = [t for t in self._fails.get(key, ()) if t > now - self.window]
            return len(recent) >= self.limit

    def fail(self, key: str, now: float | None = None) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(now if now is not None else time.monotonic())
