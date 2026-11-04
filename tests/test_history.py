import json
import threading

from app.backend.history import History
from app.backend.models import SpotifyCollection


def coll(id="a1", kind="album", title="Alb") -> SpotifyCollection:
    return SpotifyCollection(kind, id, title, "Art", "https://img.example/c.jpg", ())


def test_empty_when_file_missing(tmp_path):
    assert History(tmp_path / "h.json").recent() == []


def test_add_persists_newest_first(tmp_path):
    path = tmp_path / "sub" / "h.json"
    h = History(path)
    h.add("ana", coll("a1", title="One"), "FLAC")
    h.add("bia", coll("p2", "playlist", "Two"))
    items = History(path).recent()
    assert [(e["user"], e["title"]) for e in items] == [("bia", "Two"), ("ana", "One")]
    assert items[1] == {"user": "ana", "kind": "album", "title": "One", "subtitle": "Art",
                        "cover_url": "https://img.example/c.jpg",
                        "url": "https://open.spotify.com/album/a1", "quality": "FLAC",
                        "at": items[1]["at"]}
    assert items[0]["quality"] is None
    assert isinstance(items[1]["at"], int)


def test_same_user_same_collection_moves_to_top(tmp_path):
    h = History(tmp_path / "h.json")
    h.add("ana", coll("a1"))
    h.add("ana", coll("a2"))
    h.add("bia", coll("a1"))  # someone else: its own entry
    h.add("ana", coll("a1"))
    assert [(e["user"], e["url"][-2:]) for e in h.recent()] == [("ana", "a1"), ("bia", "a1"), ("ana", "a2")]


def test_limit_drops_oldest(tmp_path):
    path = tmp_path / "h.json"
    h = History(path, limit=3)
    for i in range(5):
        h.add("ana", coll(f"a{i}"))
    assert [e["url"][-2:] for e in h.recent()] == ["a4", "a3", "a2"]
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 3


def test_recent_returns_copies(tmp_path):
    h = History(tmp_path / "h.json")
    h.add("ana", coll())
    h.recent()[0]["title"] = "hacked"
    assert h.recent()[0]["title"] == "Alb"


def test_corrupt_file_starts_empty(tmp_path, caplog):
    path = tmp_path / "h.json"
    for bad in ("{not json", '{"a": 1}', "[1, 2]"):
        path.write_text(bad, encoding="utf-8")
        h = History(path)
        assert h.recent() == []
    assert "starting empty" in caplog.text
    h.add("ana", coll())
    assert len(History(path).recent()) == 1


def test_concurrent_adds(tmp_path):
    path = tmp_path / "h.json"
    h = History(path)
    threads = [threading.Thread(target=h.add, args=(f"u{i}", coll(f"a{i}"))) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(History(path).recent()) == 20
