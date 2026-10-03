"""Turn a Spotify URL into a SpotifyCollection using the public embed page.

The Web API no longer exposes ISRCs or third-party playlists to development
apps (Feb 2026), so we read the anonymous embed page instead. Its tracklist
has title, artists and duration but no ISRC, and playlists are capped at 100.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import requests

from app.backend.models import SpotifyCollection, TrackCandidate

KINDS = ("track", "album", "playlist")
EMBED_PLAYLIST_LIMIT = 100
SPOTIFY_HOSTS = {"open.spotify.com", "spotify.link", "link.tospotify.com"}
SHORTLINK_HOSTS = {"spotify.link", "link.tospotify.com"}

_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_URI_RE = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


class SpotifyError(Exception):
    """Raised with a message that is safe to show to the user."""


def parse_url(url: str, session: requests.Session | None = None) -> tuple[str, str]:
    """Return (kind, id) for a Spotify track/album/playlist URL or URI."""
    url = (url or "").strip()
    m = _URI_RE.match(url)
    if m:
        return m.group(1), m.group(2)

    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if host not in SPOTIFY_HOSTS:
        raise SpotifyError("Not a Spotify link.")

    if host in SHORTLINK_HOSTS:
        return parse_url(_follow_shortlink(url, session or requests.Session()))

    # /track/<id>, /intl-pt/album/<id>, /embed/playlist/<id>
    parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(parts[:-1]):
        if part in KINDS and _ID_RE.match(parts[i + 1]):
            return part, parts[i + 1]
    raise SpotifyError("Only Spotify track, album and playlist links are supported.")


def _follow_shortlink(url: str, session: requests.Session) -> str:
    # Follow redirects by hand so we never leave Spotify's hosts.
    for _ in range(5):
        resp = session.get(url, allow_redirects=False, timeout=10,
                           headers={"User-Agent": USER_AGENT})
        location = resp.headers.get("location")
        if not location:
            break
        if (urlparse(location).hostname or "").lower() not in SPOTIFY_HOSTS:
            raise SpotifyError("Short link redirects outside Spotify.")
        url = location
        if urlparse(url).hostname == "open.spotify.com":
            return url
    raise SpotifyError("Could not resolve Spotify short link.")


class SpotifyResolver:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def resolve(self, url: str) -> SpotifyCollection:
        kind, sid = parse_url(url, self.session)
        resp = self.session.get(f"https://open.spotify.com/embed/{kind}/{sid}",
                                headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 404:
            raise SpotifyError(f"Spotify {kind} not found (it may be private).")
        resp.raise_for_status()
        return parse_embed(kind, sid, resp.text)


def parse_embed(kind: str, sid: str, html: str) -> SpotifyCollection:
    m = _NEXT_DATA_RE.search(html)
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SpotifyError("Spotify embed format changed; the resolver needs updating.") from None

    title = entity.get("name") or entity.get("title") or ""
    cover = _cover_url(entity)

    if kind == "track":
        artists = tuple(a["name"] for a in entity.get("artists") or [] if a.get("name"))
        track = TrackCandidate(
            spotify_id=entity.get("id") or sid,
            title=title,
            artists=artists,
            album=None,
            duration_ms=int(entity.get("duration") or 0),
        )
        return SpotifyCollection(kind, sid, title, ", ".join(artists), cover, (track,))

    album = title if kind == "album" else None
    tracks = []
    for pos, item in enumerate(entity.get("trackList") or [], start=1):
        uri = item.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            continue  # podcasts, local files
        tracks.append(TrackCandidate(
            spotify_id=uri.rsplit(":", 1)[-1],
            title=item.get("title") or "",
            artists=split_artists(item.get("subtitle") or ""),
            album=album,
            duration_ms=int(item.get("duration") or 0),
            track_number=pos if kind == "album" else None,
        ))

    truncated = kind == "playlist" and len(entity.get("trackList") or []) >= EMBED_PLAYLIST_LIMIT
    return SpotifyCollection(kind, sid, title, entity.get("subtitle") or "", cover,
                             tuple(tracks), truncated)


def split_artists(subtitle: str) -> tuple[str, ...]:
    # The embed joins artists with ", ", which keeps names such as
    # "Tyler, The Creator" (plain space) intact.
    return tuple(a.strip() for a in subtitle.split(", ") if a.strip())


def _cover_url(entity: dict) -> str | None:
    images = (entity.get("visualIdentity") or {}).get("image") or []
    if images:
        return max(images, key=lambda i: i.get("maxWidth") or 0).get("url")
    sources = (entity.get("coverArt") or {}).get("sources") or []
    return sources[0].get("url") if sources else None
