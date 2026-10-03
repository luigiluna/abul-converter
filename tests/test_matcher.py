import json
from pathlib import Path

import pytest

from app.backend.matching.matcher import DeezerMatcher, decide, duration_score, score
from app.backend.models import DeezerTrack, MatchStatus, TrackCandidate
from app.backend.spotify.resolver import parse_embed
from tests.golden_cases import HARD_CASES
from tests.recording_api import ReplayApi

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def matcher():
    return DeezerMatcher(ReplayApi(FIXTURES / "deezer" / "golden.json"))


def cand(title="Song", artists=("Artist",), dur_ms=200_000, album=None):
    return TrackCandidate("sp", title, artists, album, dur_ms)


def dz(id, title="Song", artist="Artist", dur=200, isrc=None, album="Album"):
    return DeezerTrack(id, title, artist, album, dur, isrc)


# --- scoring ---------------------------------------------------------------

def test_duration_score_steps():
    assert duration_score(200_000, 201) == 1.0
    assert duration_score(200_000, 204) == 0.7
    assert duration_score(200_000, 209) == 0.3
    assert duration_score(200_000, 260) == 0.0
    assert duration_score(0, 200) == 0.5


def test_exact_match_scores_one():
    assert score(cand(), dz(1)) == 1.0


def test_version_mismatch_is_penalised():
    assert score(cand(), dz(1, "Song (Remix)")) <= 0.75


def test_word_order_matters():
    assert score(cand("Hate It Or Love It"), dz(1, "Love It or Hate It")) < 0.85


def test_cover_act_with_similar_name_is_penalised():
    assert score(cand(artists=("Keane",)), dz(1, artist="Keane Fans")) < 0.85


def test_foreign_featured_artist_is_penalised():
    c = cand("Umbrella", ("Rihanna", "JAY-Z"))
    assert score(c, dz(1, "Umbrella (feat. Jayme Claire)", "Rihanna")) < 0.9
    assert score(c, dz(2, "Umbrella (feat. JAY-Z)", "Rihanna")) == 1.0


# --- decision --------------------------------------------------------------

def test_same_recording_on_several_releases_is_not_ambiguous():
    r = decide(cand(), [dz(1, isrc="X"), dz(2, isrc="X"), dz(3, dur=203)], "search")
    assert r.status is MatchStatus.MATCHED


def test_two_different_recordings_with_equal_score_are_ambiguous():
    r = decide(cand(dur_ms=0), [dz(1, dur=200), dz(2, dur=240)], "search")
    assert r.status is MatchStatus.AMBIGUOUS
    assert len(r.alternatives) == 2


def test_nothing_good_is_not_found():
    r = decide(cand(), [dz(1, "Something Else", "Other")], "search")
    assert r.status is MatchStatus.NOT_FOUND
    assert decide(cand(), [], "search").status is MatchStatus.NOT_FOUND


def test_isrc_path(matcher):
    class Api:
        def track_by_isrc(self, isrc):
            return dz(9, dur=200)

    m = DeezerMatcher(Api())
    c = TrackCandidate("sp", "Song", ("Artist",), None, 200_000, isrc="ABC")
    r = m.match_track(c)
    assert (r.status, r.method, r.track.id) == (MatchStatus.MATCHED, "isrc", 9)

    far = TrackCandidate("sp", "Song", ("Artist",), None, 300_000, isrc="ABC")
    assert m.match_track(far).status is MatchStatus.AMBIGUOUS


# --- golden set (recorded Deezer responses, results checked by hand) --------

@pytest.mark.parametrize("c,status,expect", HARD_CASES, ids=[c.spotify_id for c, *_ in HARD_CASES])
def test_hard_cases(matcher, c, status, expect):
    r = matcher.match_track(c)
    assert r.status.value == status
    if expect:
        assert expect in f"{r.track.artist} — {r.track.title} [{r.track.album}]"


@pytest.mark.parametrize("kind", ["album", "playlist"])
def test_reviewed_collections_do_not_regress(matcher, kind):
    expected = json.loads((FIXTURES / "deezer" / "expected_matches.json").read_text())[kind]
    html = (FIXTURES / "spotify" / f"embed_{kind}.html").read_text(encoding="utf-8")
    tracks = list(parse_embed(kind, "x", html).tracks)
    results = matcher.match_album(tracks) if kind == "album" else [matcher.match_track(t) for t in tracks]
    got = {t.spotify_id: [r.status.value, r.track.id if r.status is MatchStatus.MATCHED else None]
           for t, r in zip(tracks, results)}
    assert got == expected


def test_album_match_uses_album_tracks(matcher):
    html = (FIXTURES / "spotify" / "embed_album.html").read_text(encoding="utf-8")
    results = matcher.match_album(list(parse_embed("album", "x", html).tracks))
    assert sum(r.method == "album" for r in results) == 16
    assert {r.track.album for r in results if r.method == "album"} == {"Global Warming (Deluxe Version)"}
