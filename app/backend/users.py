"""Per-user preferences and the optional personal ARL.

The default path is always DEFAULT_ARL. A user who saved their own ARL uses it
instead, for themselves only. The ARL is stored AES-GCM encrypted with a key
derived from APP_SECRET_KEY and is never returned in plain text except by
arl_for(), which feeds the downloader.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from Crypto.Cipher import AES

log = logging.getLogger(__name__)

USERNAME_RE = r"^[a-z0-9_-]{1,32}$"  # the name becomes a file name
QUALITIES = ("FLAC", "MP3_320", "MP3_128")


def valid_username(user: str) -> bool:
    return isinstance(user, str) and re.fullmatch(USERNAME_RE, user) is not None


def encrypt(secret_key: str, plain: str) -> str:
    cipher = AES.new(hashlib.sha256(secret_key.encode()).digest(), AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(plain.encode())
    return base64.b64encode(cipher.nonce + tag + ciphertext).decode()


def decrypt(secret_key: str, token: str) -> str:
    """Raises ValueError if the key is wrong or the data was tampered with."""
    raw = base64.b64decode(token)
    nonce, tag, ciphertext = raw[:16], raw[16:32], raw[32:]
    cipher = AES.new(hashlib.sha256(secret_key.encode()).digest(), AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode()


@dataclass
class UserPrefs:
    quality: str = "FLAC"
    concurrency: int = 2


class UserStore:
    def __init__(self, users_dir: Path, secret_key: str, max_concurrency: int = 3):
        self.users_dir = Path(users_dir)
        self._secret_key = secret_key
        self.max_concurrency = max_concurrency
        self._lock = threading.Lock()

    def get(self, user: str) -> UserPrefs:
        data = self._load(user)
        prefs = UserPrefs()
        if data.get("quality") in QUALITIES:
            prefs.quality = data["quality"]
        c = data.get("concurrency")
        if isinstance(c, int) and 1 <= c <= self.max_concurrency:
            prefs.concurrency = c
        return prefs

    def update(self, user: str, quality: str | None = None, concurrency: int | None = None) -> UserPrefs:
        if quality is not None and quality not in QUALITIES:
            raise ValueError(f"quality must be one of {', '.join(QUALITIES)}")
        if concurrency is not None and (isinstance(concurrency, bool) or not isinstance(concurrency, int)
                                        or not 1 <= concurrency <= self.max_concurrency):
            raise ValueError(f"concurrency must be between 1 and {self.max_concurrency}")
        with self._lock:
            data = self._load(user)
            if quality is not None:
                data["quality"] = quality
            if concurrency is not None:
                data["concurrency"] = concurrency
            self._save(user, data)
        return self.get(user)

    def set_arl(self, user: str, arl: str | None) -> None:
        """Save the user's own ARL; None (or blank) removes it."""
        arl = (arl or "").strip()
        with self._lock:
            data = self._load(user)
            if arl:
                data["arl_enc"] = encrypt(self._secret_key, arl)
            else:
                data.pop("arl_enc", None)
            self._save(user, data)

    def has_own_arl(self, user: str) -> bool:
        return self._own_arl(user) is not None

    def arl_for(self, user: str, default_arl: str | None) -> str | None:
        return self._own_arl(user) or default_arl

    def _own_arl(self, user: str) -> str | None:
        token = self._load(user).get("arl_enc")
        if not token:
            return None
        try:
            return decrypt(self._secret_key, token)
        except (ValueError, TypeError):
            log.warning("could not decrypt the saved ARL of user %s (APP_SECRET_KEY changed?); "
                        "using the default", user)
            return None

    def _path(self, user: str) -> Path:
        if not valid_username(user):
            raise ValueError("invalid user name")
        return self.users_dir / f"{user}.json"

    def _load(self, user: str) -> dict:
        path = self._path(user)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("user file %s unreadable (%s); using defaults", path.name, type(e).__name__)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, user: str, data: dict) -> None:
        path = self._path(user)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
