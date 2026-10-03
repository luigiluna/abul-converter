"""Data types shared between the resolver, matcher, jobs and downloader."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class TrackCandidate:
    """A track as described by Spotify. Knows nothing about Deezer."""

    spotify_id: str
    title: str
    artists: tuple[str, ...]
    album: str | None
    duration_ms: int
    isrc: str | None = None
    track_number: int | None = None
    disc_number: int | None = None


@dataclass(frozen=True)
class SpotifyCollection:
    kind: str  # "track" | "album" | "playlist"
    id: str
    title: str
    subtitle: str
    cover_url: str | None
    tracks: tuple[TrackCandidate, ...]
    truncated: bool = False


@dataclass(frozen=True)
class DeezerTrack:
    id: int
    title: str
    artist: str
    album: str
    duration_s: int
    isrc: str | None = None
    album_id: int | None = None
    track_number: int | None = None
    disc_number: int | None = None


class MatchStatus(str, Enum):
    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


@dataclass
class MatchResult:
    status: MatchStatus
    track: DeezerTrack | None = None
    score: float = 0.0
    method: str = ""  # "isrc" | "album" | "search" | "cache" | "manual"
    alternatives: list[tuple[DeezerTrack, float]] = field(default_factory=list)


@dataclass(frozen=True)
class DownloadItem:
    deezer_id: int
    output_dir: str
    track_number: int | None = None
    disc_number: int | None = None
    label: str = ""
