import pytest

from app.backend.matching.normalize import featured_artists, norm, split_version


@pytest.mark.parametrize("text,expected", [
    ("Beyoncé", "beyonce"),
    ("Don’t Stop", "dont stop"),
    ("Florence + The Machine", "florence the machine"),
    ("Simon & Garfunkel", "simon and garfunkel"),
    ("  AC/DC  ", "ac dc"),
    ("JAŸ-Z", "jay z"),
])
def test_norm(text, expected):
    assert norm(text) == expected


@pytest.mark.parametrize("title,base,tags", [
    ("Never Gonna Give You Up", "never gonna give you up", set()),
    ("Never Gonna Give You Up (Cake Mix)", "never gonna give you up", {"remix", "cake"}),
    ("Never Gonna Give You Up (Pianoforte)", "never gonna give you up", {"pianoforte"}),
    ("Song (feat. Someone)", "song", set()),
    ("Song [with Someone]", "song", set()),
    ("Song ft. Someone", "song", set()),
    ("Song - 2011 Remaster", "song", set()),
    ("Song (Remastered 2009)", "song", set()),
    ("Song - Remastered Version", "song", set()),
    ("Song - Single Version", "song", set()),
    ("Song (Original Mix)", "song", set()),
    ("Song (Radio Edit)", "song", {"edit", "radio"}),
    ("Song - Live", "song", {"live"}),
    ("Bohemian Rhapsody - Live Aid", "bohemian rhapsody", {"live", "aid"}),
    ("Bohemian Rhapsody (Live, European Tour / 1979)", "bohemian rhapsody", {"live", "european tour"}),
    ("Love Story (Taylor’s Version)", "love story", {"taylors version"}),
    ("Song (Acoustic)", "song", {"acoustic"}),
    ("Song - Benny Benassi Remix", "song", {"remix", "benny benassi"}),
    ("Song (Instrumental)", "song", {"instrumental"}),
    ("Song (Sped Up)", "song", {"sped up", "up"}),
    ("(I Can't Get No) Satisfaction", "satisfaction", {"i cant get no"}),
    ("(Untitled)", "untitled", {"untitled"}),
])
def test_split_version(title, base, tags):
    assert split_version(title) == (base, frozenset(tags))


def test_spotify_and_deezer_spellings_agree():
    # Spotify uses " - suffix", Deezer uses "(suffix)".
    assert split_version('Back in Time - featured in "Men In Black 3"') == \
        split_version('Back in Time (featured in "Men In Black 3")')
    assert split_version("Hotel California - 2013 Remaster") == \
        split_version("Hotel California (2013 Remaster)")


@pytest.mark.parametrize("title,names", [
    ("Umbrella (feat. Jayme Claire)", ["jayme claire"]),
    ("Party Ain't Over (feat. Usher & Afrojack)", ["usher", "afrojack"]),
    ("Song [with A, B and C]", ["a", "b", "c"]),
    ("Song", []),
])
def test_featured_artists(title, names):
    assert featured_artists(title) == names
