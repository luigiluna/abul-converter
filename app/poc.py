"""Proof of concept: Spotify URL -> Deezer matches -> download, no web UI.

    python -m app.poc <spotify-url>                    # analyse only
    python -m app.poc <spotify-url> --download         # also download MATCHED tracks

The ARL comes from DEFAULT_ARL in the .env file at the project root (or the
environment).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from collections import Counter
from pathlib import Path

from app.backend.deezer.api import DeezerPublicApi
from app.backend.matching.cache import MatchCache
from app.backend.matching.collection import match_collection
from app.backend.matching.matcher import DeezerMatcher
from app.backend.models import MatchStatus
from app.backend.spotify.resolver import SpotifyError, SpotifyResolver

ICONS = {MatchStatus.MATCHED: "✓", MatchStatus.AMBIGUOUS: "!", MatchStatus.NOT_FOUND: "✕"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url")
    p.add_argument("--download", action="store_true")
    p.add_argument("--quality", default="FLAC", choices=["FLAC", "MP3_320", "MP3_128"])
    p.add_argument("--out", default="downloads")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--take-ambiguous", action="store_true",
                   help="download the best guess for AMBIGUOUS tracks too")
    p.add_argument("--cache", default="data/match_cache.json")
    p.add_argument("--no-cache", action="store_true", help="ignore and don't update the match cache")
    args = p.parse_args()
    # Windows consoles default to cp1252, which cannot print ✓ ✕ —.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)s %(message)s")

    try:
        collection = SpotifyResolver().resolve(args.url)
    except SpotifyError as e:
        print(f"Spotify: {e}")
        return 1

    print(f"\n{collection.kind.upper()}: {collection.title} — {collection.subtitle} "
          f"({len(collection.tracks)} tracks)")
    if collection.truncated:
        print("  ⚠ Spotify only exposes the first 100 tracks of a playlist anonymously.")

    tracks = list(collection.tracks)
    cache = None if args.no_cache else MatchCache(Path(args.cache))
    results = match_collection(collection, DeezerMatcher(DeezerPublicApi()), cache)

    for i, (c, r) in enumerate(zip(tracks, results), 1):
        target = f"{r.track.artist} — {r.track.title} [{r.track.album}]" if r.track else ""
        print(f"{i:3}. {ICONS[r.status]} {r.score:.2f} {', '.join(c.artists)} — {c.title}"
              + (f"\n          → {target}" if target else ""))
    print("\n", dict(Counter(r.status.value for r in results)))

    if not args.download:
        return 0
    return download(args, collection, results)


def download(args, collection, results) -> int:
    from app.backend.downloads.adapter import ORDER, AuthError, DeezloadAdapter
    from app.backend.downloads.layout import plan_downloads, write_m3u8
    from app.backend.settings import default_arl

    arl = default_arl()
    if not arl:
        print("Put DEFAULT_ARL=<your arl> in the .env file at the project root to download.")
        return 1

    if args.take_ambiguous:
        for r in results:
            if r.status is MatchStatus.AMBIGUOUS:
                r.status = MatchStatus.MATCHED
    planned = plan_downloads(collection, results, os.path.abspath(args.out))
    if not planned:
        print("Nothing to download.")
        return 0

    labels = [item.label for _, item in planned]
    lock = threading.Lock()

    def on_update(u):
        if u.state in ("done", "failed", "cancelled"):
            with lock:
                extra = f" (em {u.quality})" if u.fallback_quality else ""
                extra += f": {u.error}" if u.error else ""
                print(f"  [{u.state:9}] {labels[u.index]}{extra}")

    adapter = DeezloadAdapter()
    try:
        account = adapter.account_info(arl)
    except AuthError as e:
        print(f"Deezer login failed: {e}")
        return 1
    print(f"\nDeezer account: {account.name} (plan allows up to {account.max_quality})")
    if ORDER.index(account.max_quality) > ORDER.index(args.quality):
        print(f"  ⚠ This account cannot stream {args.quality}; downloading {account.max_quality}. "
              "Use the ARL of a Premium/HiFi account for FLAC.")

    cancel = threading.Event()
    print(f"\nDownloading {len(planned)} tracks as {args.quality} to {args.out} … (Ctrl+C cancels)")
    outcome = []
    worker = threading.Thread(target=lambda: outcome.extend(_run(
        adapter, arl, args, [item for _, item in planned], on_update, cancel)))
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.2)
    except KeyboardInterrupt:
        print("\nCancelling…")
        cancel.set()
        worker.join()

    if outcome and isinstance(outcome[0], AuthError):
        print(f"Deezer login failed: {outcome[0]}")
        return 1
    m3u = write_m3u8(collection, [u.path for u in outcome])
    if m3u:
        print(f"Playlist file: {m3u}")
    print(dict(Counter(u.state for u in outcome)))
    return 0


def _run(adapter, arl, args, items, on_update, cancel):
    from app.backend.downloads.adapter import AuthError
    try:
        return adapter.run(arl, args.quality, args.concurrency, items, on_update, cancel)
    except AuthError as e:
        return [e]


if __name__ == "__main__":
    sys.exit(main())
