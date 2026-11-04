"""Thin wrapper around vendor/deezload's DeezerDownloader.

We only use DeezerDownloader.download_track(); album/playlist layout is ours
because Spotify collections do not map 1:1 to Deezer albums. Deezload is not
modified: its CLI logging is redirected by replacing the module-level log().
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.backend.models import DownloadItem

VENDOR_DIR = Path(__file__).resolve().parents[3] / "vendor" / "deezload"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

import deezload  # noqa: E402

log = logging.getLogger("spot2deez.deezload")
_tls = threading.local()
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warn": logging.WARNING, "error": logging.ERROR}


def _deezload_log(msg: str, level: str = "info") -> None:
    text = str(msg).strip().lstrip("✓✗⚠⏭↻ ").strip()
    if not text:
        return
    log.log(_LEVELS.get(level, logging.INFO), text)
    if level in ("warn", "error"):
        _tls.last_error = text


deezload.log = _deezload_log
deezload._QUIET = True

QUALITIES = {q.value: q for q in deezload.AudioQuality}
ORDER = ["FLAC", "MP3_320", "MP3_128"]  # best first
# A pooled login idle for longer than this is dropped and done again: Deezer lets a
# session go stale after some days, and a stale one refuses every track (2026-10-27).
SESSION_IDLE_SECONDS = 3600


class AuthError(Exception):
    pass


class DownloadCancelled(Exception):
    pass


@dataclass
class ItemUpdate:
    index: int
    state: str  # queued | downloading | done | failed | cancelled
    progress: float = 0.0
    path: str | None = None
    quality: str | None = None  # quality actually downloaded
    fallback_quality: bool = False
    error: str | None = None


def actual_quality(path: str) -> str:
    """FLAC / MP3_320 / MP3_128, read from the file (Deezload's 320 -> 128
    fallback keeps the .mp3 extension, so the bitrate is the only signal)."""
    if path.lower().endswith(".flac"):
        return "FLAC"
    try:
        from mutagen.mp3 import MP3
        return "MP3_320" if MP3(path).info.bitrate >= 256_000 else "MP3_128"
    except Exception:
        return "MP3"


@dataclass(frozen=True)
class AccountInfo:
    name: str
    max_quality: str  # best quality the plan allows (Deezer Free: MP3_128)


class DeezloadAdapter:
    def __init__(self, max_parallel: int = 4, max_retries: int = 3, fetch_lyrics: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self._slots = threading.BoundedSemaphore(max_parallel)
        self._max_retries = max_retries
        self._fetch_lyrics = fetch_lyrics
        self._clock = clock
        # (sha256(arl), quality) -> (downloader, last time it was handed out)
        self._pool: dict[tuple[str, str], tuple[deezload.DeezerDownloader, float]] = {}
        self._pool_lock = threading.Lock()

    def _downloader(self, arl: str, quality: str) -> "deezload.DeezerDownloader":
        # Quality lives on the instance, so instances are keyed by (ARL, quality).
        key = (hashlib.sha256(arl.encode()).hexdigest(), quality)
        with self._pool_lock:
            now = self._clock()
            dl, last_used = self._pool.get(key, (None, now))
            if dl is not None and now - last_used > SESSION_IDLE_SECONDS:
                dl = None  # a run still holding the old instance keeps using it
            if dl is None:
                dl = deezload.DeezerDownloader(
                    arl, quality=QUALITIES[quality], max_retries=self._max_retries,
                    fetch_lyrics=self._fetch_lyrics)
                _tls.last_error = None
                if not dl.authenticate():
                    raise AuthError(getattr(_tls, "last_error", None)
                                    or "Deezer login failed; the ARL may be invalid or expired.")
            self._pool[key] = (dl, now)
            return dl

    def account_info(self, arl: str) -> AccountInfo:
        """Log in (cached) and read what the account's plan may stream. Raises AuthError."""
        user = self._downloader(arl, "MP3_128").client.current_user
        if user.get("can_stream_lossless"):
            best = "FLAC"
        elif user.get("can_stream_hq"):
            best = "MP3_320"
        else:
            best = "MP3_128"
        return AccountInfo(user.get("name") or "", best)

    def run(self, arl: str, quality: str, concurrency: int, items: list[DownloadItem],
            on_update: Callable[[ItemUpdate], None], cancel: threading.Event) -> list[ItemUpdate]:
        """Download items; blocks until done or cancelled. Raises AuthError.

        The request is capped at the account's best allowed quality, so Deezload
        does not waste retries on tiers Deezer will refuse for this plan.
        """
        if quality not in QUALITIES:
            raise ValueError(f"Unknown quality {quality!r}")
        cap = self.account_info(arl).max_quality
        effective = ORDER[max(ORDER.index(quality), ORDER.index(cap))]
        dl = self._downloader(arl, effective)

        def work(index: int, item: DownloadItem) -> ItemUpdate:
            while not self._slots.acquire(timeout=0.5):
                if cancel.is_set():
                    return _emit(ItemUpdate(index, "cancelled"))
            try:
                if cancel.is_set():
                    return _emit(ItemUpdate(index, "cancelled"))
                _tls.last_error = None
                last_emit = 0.0

                def progress(pct: float) -> None:
                    nonlocal last_emit
                    if cancel.is_set():
                        raise DownloadCancelled()
                    now = time.monotonic()
                    if now - last_emit >= 0.25:
                        last_emit = now
                        on_update(ItemUpdate(index, "downloading", round(pct, 1)))

                on_update(ItemUpdate(index, "downloading", 0.0))
                path = dl.download_track(str(item.deezer_id), item.output_dir,
                                         track_num=item.track_number,
                                         disc_num=item.disc_number, progress_cb=progress)
                if path:
                    # Deezload steps down FLAC -> MP3_320 -> MP3_128, stopping at
                    # the best quality the track actually has.
                    got = actual_quality(path)
                    unknown_mp3 = got == "MP3" and quality != "FLAC"
                    return _emit(ItemUpdate(index, "done", 100.0, path, got,
                                            fallback_quality=got != quality and not unknown_mp3))
                if cancel.is_set():
                    return _emit(ItemUpdate(index, "cancelled"))
                return _emit(ItemUpdate(index, "failed",
                                        error=getattr(_tls, "last_error", None) or "Download failed"))
            except Exception as e:  # never let one track kill the batch
                return _emit(ItemUpdate(index, "failed", error=str(e)))
            finally:
                self._slots.release()

        def _emit(update: ItemUpdate) -> ItemUpdate:
            on_update(update)
            return update

        for i in range(len(items)):
            on_update(ItemUpdate(i, "queued"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            return list(pool.map(lambda args: work(*args), enumerate(items)))
