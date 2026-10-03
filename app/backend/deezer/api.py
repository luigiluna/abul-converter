"""Small client for Deezer's public API (no login needed)."""

from __future__ import annotations

import threading
import time

import requests

from app.backend.models import DeezerTrack

API = "https://api.deezer.com"
# Deezer allows roughly 50 requests per 5 seconds per IP.
MIN_INTERVAL_S = 0.11
QUOTA_ERROR_CODE = 4


class DeezerApiError(Exception):
    pass


class DeezerPublicApi:
    def __init__(self, session: requests.Session | None = None, max_retries: int = 4):
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + MIN_INTERVAL_S
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(f"{API}/{path}", params=params, timeout=15)
            except requests.RequestException as e:
                error: Exception = e
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    error = DeezerApiError(f"HTTP {resp.status_code}")
                else:
                    data = resp.json()
                    err = data.get("error") if isinstance(data, dict) else None
                    if not err:
                        return data
                    if err.get("code") != QUOTA_ERROR_CODE:
                        # Not found etc. — callers treat this as "no result".
                        raise DeezerApiError(err.get("message") or str(err))
                    error = DeezerApiError("quota exceeded")
            time.sleep(min(2 ** attempt, 8))
        raise DeezerApiError(f"Deezer API unavailable: {error}")

    def track_by_isrc(self, isrc: str) -> DeezerTrack | None:
        try:
            data = self._get(f"track/isrc:{isrc}")
        except DeezerApiError:
            return None
        return to_track(data) if data.get("id") else None

    def search_tracks(self, query: str, limit: int = 25) -> list[DeezerTrack]:
        data = self._get("search", {"q": query, "limit": limit})
        return [to_track(t) for t in data.get("data", [])]

    def search_albums(self, query: str, limit: int = 10) -> list[dict]:
        return self._get("search/album", {"q": query, "limit": limit}).get("data", [])

    def album_tracks(self, album_id: int, album_title: str = "") -> list[DeezerTrack]:
        data = self._get(f"album/{album_id}/tracks", {"limit": 500})
        return [to_track(t, album_id=album_id, album_title=album_title)
                for t in data.get("data", [])]


def to_track(t: dict, album_id: int | None = None, album_title: str = "") -> DeezerTrack:
    album = t.get("album") or {}
    return DeezerTrack(
        id=int(t["id"]),
        title=t.get("title") or "",
        artist=(t.get("artist") or {}).get("name") or "",
        album=album.get("title") or album_title,
        duration_s=int(t.get("duration") or 0),
        isrc=t.get("isrc") or None,
        album_id=album.get("id") or album_id,
        track_number=t.get("track_position"),
        disc_number=t.get("disk_number"),
    )
