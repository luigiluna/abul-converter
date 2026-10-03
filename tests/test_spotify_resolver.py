from pathlib import Path

import pytest

from app.backend.spotify.resolver import SpotifyError, parse_embed, parse_url, split_artists

FIXTURES = Path(__file__).parent / "fixtures" / "spotify"
ID = "4cOdK2wGLETKBW3PvgPWqT"


def embed(kind):
    return (FIXTURES / f"embed_{kind}.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("url,expected", [
    (f"https://open.spotify.com/track/{ID}", ("track", ID)),
    (f"https://open.spotify.com/track/{ID}?si=abc&utm_source=x", ("track", ID)),
    (f"https://open.spotify.com/intl-pt/album/{ID}", ("album", ID)),
    (f"https://open.spotify.com/embed/playlist/{ID}", ("playlist", ID)),
    (f"open.spotify.com/playlist/{ID}/", ("playlist", ID)),
    (f"spotify:track:{ID}", ("track", ID)),
    (f"  https://open.spotify.com/album/{ID}  ", ("album", ID)),
])
def test_parse_url(url, expected):
    assert parse_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://www.deezer.com/track/3135556",
    f"https://open.spotify.com/artist/{ID}",
    f"https://open.spotify.com/episode/{ID}",
    "https://open.spotify.com/track/short",
    f"https://evil.example/open.spotify.com/track/{ID}",
    "",
])
def test_parse_url_rejects(url):
    with pytest.raises(SpotifyError):
        parse_url(url)


def test_shortlink_must_stay_on_spotify():
    class FakeSession:
        def get(self, url, **kw):
            class R:
                headers = {"location": "https://evil.example/x"}
            return R()

    with pytest.raises(SpotifyError, match="outside Spotify"):
        parse_url("https://spotify.link/abc", FakeSession())


def test_shortlink_followed():
    class FakeSession:
        def get(self, url, **kw):
            class R:
                headers = {"location": f"https://open.spotify.com/album/{ID}?si=1"}
            return R()

    assert parse_url("https://spotify.link/abc", FakeSession()) == ("album", ID)


def test_track_embed():
    c = parse_embed("track", ID, embed("track"))
    assert c.title == "Never Gonna Give You Up"
    assert c.subtitle == "Rick Astley"
    assert c.cover_url.startswith("https://")
    (t,) = c.tracks
    assert t.artists == ("Rick Astley",)
    assert t.duration_ms == 213573
    assert t.isrc is None and t.album is None


def test_album_embed():
    c = parse_embed("album", "x", embed("album"))
    assert (c.title, c.subtitle, len(c.tracks)) == ("Global Warming", "Pitbull", 18)
    assert [t.track_number for t in c.tracks] == list(range(1, 19))
    assert all(t.album == "Global Warming" for t in c.tracks)
    assert c.tracks[0].artists == ("Pitbull", "Sensato")
    assert not c.truncated


def test_playlist_embed_is_flagged_truncated_at_100():
    c = parse_embed("playlist", "x", embed("playlist"))
    assert c.title == "All Out 2000s"
    assert len(c.tracks) == 100
    assert c.truncated
    assert all(t.album is None and t.track_number is None for t in c.tracks)


def test_changed_embed_format_gives_clear_error():
    with pytest.raises(SpotifyError, match="format changed"):
        parse_embed("track", ID, "<html>nothing here</html>")


def test_split_artists_keeps_commas_inside_names():
    assert split_artists("Tyler, The Creator, Kali Uchis") == ("Tyler, The Creator", "Kali Uchis")
    assert split_artists("") == ()
