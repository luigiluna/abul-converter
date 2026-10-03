"""Text normalisation for comparing Spotify and Deezer metadata."""

from __future__ import annotations

import re
import unicodedata

_PUNCT_MAP = str.maketrans({
    "‘": "'", "’": "'", "`": "'", "´": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-", "‐": "-",
    "&": " and ",
})
_NON_WORD = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")
_YEAR = re.compile(r"(19|20)\d\d")

# "(feat. X)", "[with X]", "ft. X" and similar collaborator credits.
_FEAT_GROUP = re.compile(r"[(\[]\s*(feat\.?|ft\.?|featuring|with)\s[^)\]]*[)\]]", re.I)
_FEAT_TAIL = re.compile(r"\s(feat\.?|ft\.?|featuring)\s.*$", re.I)
_GROUP = re.compile(r"[(\[]([^)\]]*)[)\]]")
_DASH_SUFFIX = re.compile(r"\s+-\s+(.+)$")

# Words inside "(...)" or after " - " that mean "this is a different recording".
_VERSION_WORDS = {
    "remix": "remix", "rmx": "remix", "mix": "remix", "rework": "remix", "bootleg": "remix",
    "live": "live", "unplugged": "live",
    "acoustic": "acoustic",
    "instrumental": "instrumental", "karaoke": "instrumental",
    "demo": "demo",
    "edit": "edit",
    "extended": "extended",
    "sped": "sped up", "nightcore": "sped up",
    "slowed": "slowed", "reverb": "slowed",
    "orchestral": "orchestral", "symphonic": "orchestral",
    "piano": "piano",
    "cover": "cover",
    "taylors": "taylors version",
}
# Phrases that look like versions but mean the same recording.
_NEUTRAL_PHRASES = re.compile(
    r"\b(original mix|album version|single version|original version|mono|stereo|"
    r"remaster(ed)?|deluxe( edition| version)?|bonus track|explicit|clean|version)\b")


def norm(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "").translate(_PUNCT_MAP)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("'", "")
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


_FEAT_NAMES = re.compile(r"\b(?:feat\.?|ft\.?|featuring|with)\s+([^)\]]+)", re.I)
_NAME_SEP = re.compile(r"\s*(?:,|&|\band\b|\bx\b)\s*", re.I)


def featured_artists(title: str) -> list[str]:
    """Names credited as "feat." inside a title, normalised."""
    names: list[str] = []
    for m in _FEAT_NAMES.finditer(title or ""):
        names += [norm(n) for n in _NAME_SEP.split(m.group(1)) if norm(n)]
    return names


def strip_featuring(title: str) -> str:
    title = _FEAT_GROUP.sub(" ", title or "")
    return _FEAT_TAIL.sub("", title).strip()


def split_version(title: str) -> tuple[str, frozenset[str]]:
    """Split a title into (normalised base title, version tags).

    "Never Gonna Give You Up (Cake Mix)" -> ("never gonna give you up", {"remix"})
    "Song - 2011 Remaster"               -> ("song", set())
    """
    title = strip_featuring(title).translate(_PUNCT_MAP)
    parts = [m.group(1) for m in _GROUP.finditer(title)]
    base = _GROUP.sub(" ", title)
    m = _DASH_SUFFIX.search(base)
    if m:
        parts.append(m.group(1))
        base = base[: m.start()]

    tags: set[str] = set()
    for part in parts:
        words = _NEUTRAL_PHRASES.sub(" ", norm(part)).split()
        words = [w for w in words if not _YEAR.fullmatch(w)]
        tags |= {_VERSION_WORDS[w] for w in words if w in _VERSION_WORDS}
        # Keep the rest verbatim ("Live Aid" vs "Live, European Tour",
        # "(Pianoforte)", remixer names) so both sides must carry the same one.
        rest = [w for w in words if w not in _VERSION_WORDS]
        if rest:
            tags.add(" ".join(rest))

    base_norm = norm(base)
    if not base_norm:  # title was entirely in brackets
        base_norm = norm(title)
    return base_norm, frozenset(tags)
