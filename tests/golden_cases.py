"""Hand-picked hard cases.

The Spotify side is synthetic (titles as Spotify writes them, durations
approximate); the expected Deezer results were checked by hand.
"""

from app.backend.models import TrackCandidate as T

# (candidate, expected status, substring expected in the Deezer title/artist/album or None)
HARD_CASES = [
    (T("h1", "Never Gonna Give You Up", ("Rick Astley",), None, 213573), "MATCHED", "Rick Astley — Never Gonna Give You Up ["),
    (T("h2", "Rain Over Me (feat. Marc Anthony) - Benny Benassi Remix", ("Pitbull", "Marc Anthony", "Benny Benassi"), None, 316000), "NOT_FOUND", None),
    (T("h3", "Umbrella", ("Rihanna", "JAŸ-Z"), None, 275986), "MATCHED", "Rihanna — Umbrella ["),
    (T("h4", "Hate It Or Love It", ("The Game", "50 Cent"), None, 206400), "MATCHED", "The Game — Hate It Or Love It ["),
    (T("h5", "Love Story (Taylor's Version)", ("Taylor Swift",), None, 235767), "MATCHED", "Taylor's Version"),
    (T("h6", "Love Story", ("Taylor Swift",), None, 235280), "MATCHED", "Taylor Swift — Love Story ["),
    (T("h7", "EARFQUAKE", ("Tyler, The Creator",), None, 190066), "MATCHED", "Tyler, The Creator — EARFQUAKE"),
    (T("h8", "Envolver", ("Anitta",), None, 193591), "MATCHED", "Anitta — Envolver"),
    (T("h9", "Dynamite", ("BTS",), None, 199053), "MATCHED", "BTS — Dynamite ["),
    (T("h10", "Halo", ("Beyoncé",), None, 261640), "MATCHED", "Beyoncé — Halo ["),
    (T("h11", "Somewhere Only We Know", ("Keane",), None, 237000), "MATCHED", "Keane — Somewhere Only We Know ["),
    (T("h12", "Beautiful Girls", ("Sean Kingston",), None, 225373), "AMBIGUOUS", None),
    (T("h13", "Hotel California - 2013 Remaster", ("Eagles",), None, 391376), "MATCHED", "Eagles — Hotel California"),
    (T("h14", "Bohemian Rhapsody - Live Aid", ("Queen",), None, 148000), "MATCHED", "Live Aid"),
    # Same title with the album-version length: must not pick a different live recording.
    (T("h14b", "Bohemian Rhapsody - Live Aid", ("Queen",), None, 357000), "AMBIGUOUS", "Live Aid"),
    (T("h15", "Uma Música Que Não Existe De Jeito Nenhum", ("Artista Inexistente XYZ",), None, 200000), "NOT_FOUND", None),
]
