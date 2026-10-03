"""Persistent spotify_id -> Deezer track cache, shared by every user.

Only MATCHED results are stored: AMBIGUOUS/NOT_FOUND may change as the Deezer
catalogue changes, so they are always re-matched. Manual picks win over
automatic matches and are never overwritten by them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict
from pathlib import Path

from app.backend.models import DeezerTrack, MatchResult, MatchStatus

log = logging.getLogger(__name__)


class MatchCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def get(self, spotify_id: str) -> MatchResult | None:
        with self._lock:
            entry = self._data.get(spotify_id)
        if not entry:
            return None
        try:
            track = DeezerTrack(**entry["track"])
        except (KeyError, TypeError):
            return None
        method = "manual" if entry.get("manual") else "cache"
        return MatchResult(MatchStatus.MATCHED, track, float(entry.get("score", 1.0)), method)

    def put(self, spotify_id: str, result: MatchResult) -> None:
        if result.status is not MatchStatus.MATCHED or result.track is None:
            return
        with self._lock:
            if self._data.get(spotify_id, {}).get("manual"):
                return
            self._data[spotify_id] = {"track": asdict(result.track), "score": result.score, "manual": False}
            self._save()

    def put_manual(self, spotify_id: str, track: DeezerTrack) -> None:
        with self._lock:
            self._data[spotify_id] = {"track": asdict(track), "score": 1.0, "manual": True}
            self._save()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("match cache %s unreadable (%s); starting empty", self.path, e)
            return {}
        if not isinstance(data, dict):
            log.warning("match cache %s has unexpected format; starting empty", self.path)
            return {}
        return data

    def _save(self) -> None:
        # Caller holds the lock. Write to a temp file and swap so a crash never
        # leaves a half-written cache behind.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
