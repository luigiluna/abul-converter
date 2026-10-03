import json
import threading

from app.backend.matching.cache import MatchCache
from app.backend.matching.collection import match_collection
from app.backend.models import (DeezerTrack, MatchResult, MatchStatus, SpotifyCollection,
                                TrackCandidate)


def dz(i: int) -> DeezerTrack:
    return DeezerTrack(id=i, title=f"T{i}", artist="A", album="Al", duration_s=200, isrc=f"ISRC{i}")


def cand(i: int) -> TrackCandidate:
    return TrackCandidate(spotify_id=f"sp{i}", title=f"T{i}", artists=("A",), album="Al", duration_ms=200_000)


def matched(i: int, score: float = 0.95) -> MatchResult:
    return MatchResult(MatchStatus.MATCHED, dz(i), score, "search")


# --- MatchCache --------------------------------------------------------------

def test_get_missing_returns_none(tmp_path):
    assert MatchCache(tmp_path / "c.json").get("nope") is None


def test_put_and_get_persists(tmp_path):
    path = tmp_path / "sub" / "c.json"
    MatchCache(path).put("sp1", matched(1, 0.9))

    r = MatchCache(path).get("sp1")
    assert r.status is MatchStatus.MATCHED
    assert r.track == dz(1)
    assert r.score == 0.9
    assert r.method == "cache"
    assert json.loads(path.read_text(encoding="utf-8"))["sp1"]["manual"] is False


def test_only_matched_is_cached(tmp_path):
    cache = MatchCache(tmp_path / "c.json")
    cache.put("a", MatchResult(MatchStatus.AMBIGUOUS, dz(1), 0.7, "search"))
    cache.put("b", MatchResult(MatchStatus.NOT_FOUND))
    assert cache.get("a") is None and cache.get("b") is None
    assert not (tmp_path / "c.json").exists()


def test_manual_has_priority_over_automatic(tmp_path):
    path = tmp_path / "c.json"
    cache = MatchCache(path)
    cache.put("sp1", matched(1))
    cache.put_manual("sp1", dz(2))
    cache.put("sp1", matched(3))

    r = MatchCache(path).get("sp1")
    assert r.track == dz(2)
    assert r.method == "manual"
    assert r.score == 1.0


def test_corrupt_file_starts_empty(tmp_path, caplog):
    path = tmp_path / "c.json"
    path.write_text("{not json", encoding="utf-8")
    cache = MatchCache(path)
    assert cache.get("sp1") is None
    assert "unreadable" in caplog.text

    cache.put("sp1", matched(1))
    assert MatchCache(path).get("sp1").track == dz(1)


def test_concurrent_writes(tmp_path):
    path = tmp_path / "c.json"
    cache = MatchCache(path)
    threads = [threading.Thread(target=cache.put, args=(f"sp{i}", matched(i))) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    reloaded = MatchCache(path)
    assert all(reloaded.get(f"sp{i}").track.id == i for i in range(50))
    assert not (tmp_path / "c.json.tmp").exists()


# --- match_collection --------------------------------------------------------

class FakeMatcher:
    """Matches sp<i> to Deezer id i; odd ids are NOT_FOUND."""

    def __init__(self):
        self.track_calls: list[str] = []
        self.album_calls: list[list[str]] = []
        self.lock = threading.Lock()

    def _result(self, c: TrackCandidate) -> MatchResult:
        i = int(c.spotify_id[2:])
        return matched(i) if i % 2 == 0 else MatchResult(MatchStatus.NOT_FOUND)

    def match_track(self, c):
        with self.lock:
            self.track_calls.append(c.spotify_id)
        return self._result(c)

    def match_album(self, tracks):
        self.album_calls.append([c.spotify_id for c in tracks])
        return [self._result(c) for c in tracks]


def coll(kind: str, n: int) -> SpotifyCollection:
    return SpotifyCollection(kind, "id", "Title", "Sub", None, tuple(cand(i) for i in range(n)))


def test_playlist_preserves_order_and_reports_progress(tmp_path):
    progress = []
    results = match_collection(coll("playlist", 20), FakeMatcher(), None, workers=4,
                               on_progress=lambda d, t: progress.append((d, t)))
    assert [r.track.id if r.track else None for r in results] == \
        [i if i % 2 == 0 else None for i in range(20)]
    assert progress[-1] == (20, 20)
    assert [d for d, _ in progress] == sorted(d for d, _ in progress)


def test_cache_avoids_repeat_calls(tmp_path):
    cache = MatchCache(tmp_path / "c.json")
    first = FakeMatcher()
    match_collection(coll("playlist", 6), first, cache)
    assert sorted(first.track_calls) == [f"sp{i}" for i in range(6)]

    second = FakeMatcher()
    progress = []
    results = match_collection(coll("playlist", 6), second, cache, on_progress=lambda d, t: progress.append(d))
    # Only the NOT_FOUND ones are matched again.
    assert sorted(second.track_calls) == ["sp1", "sp3", "sp5"]
    assert [r.method for r in results[::2]] == ["cache"] * 3
    assert progress[-1] == 6


def test_album_uses_match_album_only_for_uncached(tmp_path):
    cache = MatchCache(tmp_path / "c.json")
    cache.put_manual("sp1", dz(99))
    m = FakeMatcher()
    progress = []
    results = match_collection(coll("album", 4), m, cache, on_progress=lambda d, t: progress.append((d, t)))

    assert m.album_calls == [["sp0", "sp2", "sp3"]]
    assert m.track_calls == []
    assert results[1].track.id == 99 and results[1].method == "manual"
    assert results[0].track.id == 0 and results[2].track.id == 2
    assert results[3].status is MatchStatus.NOT_FOUND
    assert progress[-1] == (4, 4)


def test_empty_collection():
    assert match_collection(coll("playlist", 0), FakeMatcher(), None) == []
