"""Shared "recently downloaded" list: the one thing every user can see.

Only collection metadata is kept (who, what, cover, Spotify link) — never job
ids, tracks, files or anything about the Deezer account — so it does not weaken
the per-user isolation of jobs. A capped JSON list, because a handful of
friends do not need a database.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from app.backend.models import SpotifyCollection

log = logging.getLogger(__name__)


class History:
    def __init__(self, path: Path, limit: int = 50):
        self.path = Path(path)
        self.limit = limit
        self._lock = threading.Lock()
        self._items: list[dict] = self._load()

    def add(self, user: str, collection: SpotifyCollection, quality: str | None = None) -> None:
        entry = {"user": user, "kind": collection.kind, "title": collection.title,
                 "subtitle": collection.subtitle, "cover_url": collection.cover_url,
                 "url": f"https://open.spotify.com/{collection.kind}/{collection.id}",
                 "quality": quality, "at": int(time.time())}
        with self._lock:
            # The same user downloading the same thing again moves it to the top.
            rest = [e for e in self._items if (e.get("user"), e.get("url")) != (user, entry["url"])]
            self._items = [entry, *rest][:self.limit]
            self._save()

    def recent(self) -> list[dict]:
        """Newest first."""
        with self._lock:
            return [dict(e) for e in self._items]

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("history %s unreadable (%s); starting empty", self.path, e)
            return []
        if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
            log.warning("history %s has unexpected format; starting empty", self.path)
            return []
        return data[:self.limit]

    def _save(self) -> None:
        # Caller holds the lock. Temp file + swap, as in MatchCache.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
