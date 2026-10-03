"""Where each matched track goes on disk."""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.backend.models import DownloadItem, MatchResult, MatchStatus, SpotifyCollection

_ILLEGAL = re.compile(r'[/\\:*?"<>|]')


def safe_name(name: str) -> str:
    # Same rule as Deezload's sanitise_filename, so folder and file names agree.
    return _ILLEGAL.sub("-", name).strip(". ") or "Untitled"


def collection_dir(collection: SpotifyCollection, root: str) -> str:
    if collection.kind == "album":
        return str(Path(root) / safe_name(f"{collection.subtitle} - {collection.title}"))
    if collection.kind == "playlist":
        return str(Path(root) / safe_name(collection.title))
    return root


def plan_downloads(collection: SpotifyCollection, results: list[MatchResult],
                   root: str) -> list[tuple[int, DownloadItem]]:
    """Return (track index, DownloadItem) for every track that has a Deezer match.

    Only MATCHED results are planned; the caller turns an AMBIGUOUS result
    into MATCHED once the user has picked an alternative.
    """
    folder = collection_dir(collection, root)
    planned = []
    for i, (c, r) in enumerate(zip(collection.tracks, results)):
        if r.status is not MatchStatus.MATCHED or r.track is None:
            continue
        planned.append((i, DownloadItem(
            deezer_id=r.track.id,
            output_dir=folder,
            track_number=c.track_number if collection.kind == "album" else None,
            label=f"{', '.join(c.artists)} — {c.title}",
        )))
    return planned


def write_m3u8(collection: SpotifyCollection, paths_in_order: list[str | None]) -> str | None:
    """Write <playlist>.m3u8 next to the files, in Spotify order."""
    paths = [p for p in paths_in_order if p]
    if collection.kind != "playlist" or not paths:
        return None
    folder = Path(paths[0]).parent
    m3u = folder / f"{safe_name(collection.title)}.m3u8"
    lines = ["#EXTM3U"] + [os.path.relpath(p, folder) for p in paths]
    m3u.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(m3u)
