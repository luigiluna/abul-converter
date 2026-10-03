"""Resolve Spotify TrackCandidates to Deezer tracks with an explicit score.

Order of evidence: ISRC (rarely available) > whole-album match by
duration/title > per-track search scored on title, artist, duration, album.
Results are MATCHED only when the best recording is clearly ahead; anything
uncertain is AMBIGUOUS so the user decides.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from app.backend.deezer.api import DeezerPublicApi
from app.backend.matching.normalize import featured_artists, norm, split_version
from app.backend.models import DeezerTrack, MatchResult, MatchStatus, TrackCandidate

MATCHED_MIN = 0.85
AMBIGUOUS_MIN = 0.60
RUNNER_UP_MAX = 0.70  # a second recording above this...
MIN_MARGIN = 0.10     # ...must be at least this far behind the best
VERSION_PENALTY = 0.25
FOREIGN_FEAT_PENALTY = 0.15
SAME_RECORDING_MAX_DIFF_S = 4
ARTIST_EXACT_MIN = 0.85
ISRC_MAX_DURATION_DIFF_S = 10
ALBUM_MIN_TITLE_SIM = 0.6
ALBUM_CANDIDATES = 5


def duration_score(spotify_ms: int, deezer_s: int) -> float:
    if not spotify_ms or not deezer_s:
        return 0.5  # unknown: neither reward nor punish much
    diff = abs(spotify_ms / 1000 - deezer_s)
    if diff <= 2:
        return 1.0
    if diff <= 5:
        return 0.7
    if diff <= 10:
        return 0.3
    return 0.0


def artist_score(spotify_artists: tuple[str, ...], deezer_artist: str) -> float:
    target = norm(deezer_artist)
    if not target or not spotify_artists:
        return 0.0
    best = max(fuzz.ratio(norm(a), target) for a in spotify_artists) / 100
    # A near-miss artist ("Keane Fans", "Cover All Stars") is usually a cover act.
    return best if best >= ARTIST_EXACT_MIN else best / 2


def score(c: TrackCandidate, d: DeezerTrack) -> float:
    sp_base, sp_tags = split_version(c.title)
    dz_base, dz_tags = split_version(d.title)
    # Plain ratio: word order matters ("Hate It Or Love It" vs "Love It or Hate It").
    title = fuzz.ratio(sp_base, dz_base) / 100
    artist = artist_score(c.artists, d.artist)
    duration = duration_score(c.duration_ms, d.duration_s)

    if c.album:
        album = fuzz.token_sort_ratio(split_version(c.album)[0], split_version(d.album)[0]) / 100
        total = 0.45 * title + 0.30 * artist + 0.20 * duration + 0.05 * album
    else:
        total = 0.50 * title + 0.30 * artist + 0.20 * duration

    if sp_tags != dz_tags:
        total -= VERSION_PENALTY
    # "Umbrella (feat. Jayme Claire)" is not the Rihanna & JAY-Z recording.
    sp_artists = [norm(a) for a in c.artists]
    for name in featured_artists(d.title):
        if not any(fuzz.ratio(name, a) >= 80 for a in sp_artists):
            total -= FOREIGN_FEAT_PENALTY
            break
    return max(0.0, min(1.0, total))


def same_recording(a: DeezerTrack, b: DeezerTrack) -> bool:
    """Same song for the user's purposes: singles, albums, compilations and
    remastered editions carry different Deezer ids (and sometimes ISRCs)."""
    if a.isrc and a.isrc == b.isrc:
        return True
    return (split_version(a.title) == split_version(b.title)
            and featured_artists(a.title) == featured_artists(b.title)
            and norm(a.artist) == norm(b.artist)
            and abs(a.duration_s - b.duration_s) <= SAME_RECORDING_MAX_DIFF_S)


def decide(c: TrackCandidate, candidates: list[DeezerTrack], method: str) -> MatchResult:
    scored = sorted(((d, score(c, d)) for d in candidates), key=lambda x: x[1], reverse=True)
    # Keep only the best-scoring representative of each recording.
    ranked: list[tuple[DeezerTrack, float]] = []
    for d, s in scored:
        if not any(same_recording(d, kept) for kept, _ in ranked):
            ranked.append((d, s))

    if not ranked or ranked[0][1] < AMBIGUOUS_MIN:
        return MatchResult(MatchStatus.NOT_FOUND, score=ranked[0][1] if ranked else 0.0,
                           method=method, alternatives=ranked[:3])

    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    clear = runner_up < RUNNER_UP_MAX or best_score - runner_up >= MIN_MARGIN
    if best_score >= MATCHED_MIN and clear:
        return MatchResult(MatchStatus.MATCHED, best, best_score, method, ranked[1:3])
    return MatchResult(MatchStatus.AMBIGUOUS, best, best_score, method, ranked[:3])


class DeezerMatcher:
    def __init__(self, api: DeezerPublicApi):
        self.api = api

    def match_track(self, c: TrackCandidate) -> MatchResult:
        if c.isrc:
            d = self.api.track_by_isrc(c.isrc)
            if d:
                diff = abs(c.duration_ms / 1000 - d.duration_s) if c.duration_ms else 0
                if diff <= ISRC_MAX_DURATION_DIFF_S:
                    return MatchResult(MatchStatus.MATCHED, d, 1.0, "isrc")
                return MatchResult(MatchStatus.AMBIGUOUS, d, 0.8, "isrc", [(d, 0.8)])

        base, _ = split_version(c.title)
        primary = c.artists[0] if c.artists else ""
        # Deezer's artist:"..." filter currently returns nothing, so use free text.
        candidates = self.api.search_tracks(f"{primary} {base}".strip())
        result = decide(c, candidates, "search")
        if result.status is MatchStatus.MATCHED:
            return result

        seen = {d.id for d in candidates}
        extra = [d for d in self.api.search_tracks(f'track:"{base}"') if d.id not in seen]
        if not extra:
            return result
        return decide(c, candidates + extra, "search")

    def match_album(self, tracks: list[TrackCandidate]) -> list[MatchResult]:
        """Match an album's tracks, trying to find the whole album first."""
        results: list[MatchResult | None] = [None] * len(tracks)
        album_tracks = self._best_album_tracks(tracks)
        if album_tracks:
            for i, c in enumerate(tracks):
                r = decide(c, album_tracks, "album")
                if r.status is MatchStatus.MATCHED:
                    results[i] = r

        return [r if r else self.match_track(c) for r, c in zip(results, tracks)]

    def _best_album_tracks(self, tracks: list[TrackCandidate]) -> list[DeezerTrack]:
        if not tracks or not tracks[0].album:
            return []
        album_title = tracks[0].album
        artist = tracks[0].artists[0] if tracks[0].artists else ""
        wanted = split_version(album_title)[0]

        best: list[DeezerTrack] = []
        best_key = (0.0, 0)
        for album in self.api.search_albums(f"{artist} {album_title}")[:ALBUM_CANDIDATES]:
            title_sim = fuzz.token_sort_ratio(wanted, split_version(album.get("title", ""))[0]) / 100
            if title_sim < ALBUM_MIN_TITLE_SIM:
                continue
            dz_tracks = self.api.album_tracks(int(album["id"]), album.get("title", ""))
            matched = sum(1 for c in tracks if max((score(c, d) for d in dz_tracks), default=0) >= MATCHED_MIN)
            # Prefer more matched tracks, then the edition with the closest size.
            key = (matched / len(tracks), -abs(len(dz_tracks) - len(tracks)))
            if key > best_key:
                best, best_key = dz_tracks, key
        return best
