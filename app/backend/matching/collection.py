"""Match a whole Spotify collection, using the cache and reporting progress."""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable

from app.backend.matching.cache import MatchCache
from app.backend.matching.matcher import DeezerMatcher
from app.backend.models import MatchResult, MatchStatus, SpotifyCollection


def match_collection(collection: SpotifyCollection, matcher: DeezerMatcher, cache: MatchCache | None,
                     workers: int = 4, on_progress: Callable[[int, int], None] | None = None
                     ) -> list[MatchResult]:
    tracks = list(collection.tracks)
    total = len(tracks)
    results: list[MatchResult | None] = [cache.get(c.spotify_id) if cache else None for c in tracks]
    done = sum(r is not None for r in results)
    lock = threading.Lock()

    def finished(i: int, r: MatchResult) -> None:
        nonlocal done
        results[i] = r
        if cache and r.status is MatchStatus.MATCHED:
            cache.put(tracks[i].spotify_id, r)
        with lock:
            done += 1
            n = done
        if on_progress:
            on_progress(n, total)

    if on_progress and done:
        on_progress(done, total)

    pending = [i for i, r in enumerate(results) if r is None]
    if collection.kind == "album" and pending:
        # Album matching needs all the tracks at once to find the right edition.
        for i, r in zip(pending, matcher.match_album([tracks[i] for i in pending])):
            finished(i, r)
    elif pending:
        with concurrent.futures.ThreadPoolExecutor(max(1, workers)) as pool:
            futures = {pool.submit(matcher.match_track, tracks[i]): i for i in pending}
            for f in concurrent.futures.as_completed(futures):
                finished(futures[f], f.result())

    return results  # type: ignore[return-value]
