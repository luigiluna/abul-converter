"""DeezerPublicApi that replays (or records) responses from a JSON file."""

import json
from pathlib import Path

from app.backend.deezer.api import DeezerApiError, DeezerPublicApi


class ReplayApi(DeezerPublicApi):
    def __init__(self, path: Path, record: bool = False):
        super().__init__()
        self.path, self.record = path, record
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _get(self, path, params=None):
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        if key not in self.data:
            if not self.record:
                raise DeezerApiError(f"not recorded: {key}")
            try:
                self.data[key] = super()._get(path, params)
            except DeezerApiError as e:
                self.data[key] = {"__error__": str(e)}
        value = self.data[key]
        if "__error__" in value:
            raise DeezerApiError(value["__error__"])
        return value

    def save(self):
        slim = {k: _slim(v) for k, v in self.data.items()}
        self.path.write_text(json.dumps(slim, ensure_ascii=False, sort_keys=True, indent=0),
                             encoding="utf-8")


_KEEP = {"id", "title", "duration", "isrc", "track_position", "disk_number", "nb_tracks",
         "total", "__error__"}


def _slim(o):
    """Drop fields the matcher never reads (previews, images) to keep fixtures small."""
    if not isinstance(o, dict):
        return o
    out = {k: v for k, v in o.items() if k in _KEEP}
    if "artist" in o:
        out["artist"] = {"name": o["artist"].get("name")}
    if "album" in o:
        out["album"] = {"id": o["album"].get("id"), "title": o["album"].get("title")}
    if "data" in o:
        out["data"] = [_slim(x) for x in o["data"]]
    return out
