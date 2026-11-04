import os
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest

from app.backend import jobs as jobs_mod
from app.backend.downloads.adapter import AccountInfo, AuthError, ItemUpdate
from app.backend.jobs import Busy, DiskFull, JobManager, NoArl, NoJob, WrongPhase
from app.backend.matching.cache import MatchCache
from app.backend.models import (DeezerTrack, MatchResult, MatchStatus, SpotifyCollection,
                                TrackCandidate)
from app.backend.settings import Settings
from app.backend.spotify.resolver import SpotifyError
from app.backend.users import UserStore

ARL = "default-arl-0123456789abcdef"
OWN_ARL = "own-arl-fedcba9876543210"
URL = "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy"


def dz(i: int) -> DeezerTrack:
    return DeezerTrack(id=i, title=f"T{i}", artist="Art", album="Alb", duration_s=200)


def collection(kind="album", n=3, title="Alb") -> SpotifyCollection:
    tracks = tuple(TrackCandidate(f"sp{i}", f"T{i}", ("Art",), "Alb", 200_000, track_number=i + 1)
                   for i in range(n))
    return SpotifyCollection(kind, "x", title, "Art", None, tracks)


class FakeResolver:
    def __init__(self, coll=None, error=None):
        self.coll, self.error, self.calls = coll, error, []

    def resolve(self, uri):
        self.calls.append(uri)
        if self.error:
            raise self.error
        return self.coll


class FakeMatcher:
    """sp0 -> MATCHED 100; sp1 -> AMBIGUOUS between 101 and 201; sp2 -> NOT_FOUND."""

    def _r(self, c):
        i = int(c.spotify_id[2:])
        if i == 1:
            return MatchResult(MatchStatus.AMBIGUOUS, dz(101), 0.7, "search", [(dz(101), 0.7), (dz(201), 0.65)])
        if i == 2:
            return MatchResult(MatchStatus.NOT_FOUND)
        return MatchResult(MatchStatus.MATCHED, dz(100 + i), 0.95, "search")

    def match_track(self, c):
        return self._r(c)

    def match_album(self, tracks):
        return [self._r(c) for c in tracks]


class FakeAdapter:
    """Writes one small file per item. With a gate, blocks before the second item."""

    def __init__(self, max_quality="MP3_128", auth_error=False, gate=None):
        self.max_quality, self.auth_error, self.gate = max_quality, auth_error, gate
        self.runs = []

    def account_info(self, arl):
        if self.auth_error:
            raise AuthError(f"login failed for {arl}")
        return AccountInfo("someone", self.max_quality)

    def run(self, arl, quality, concurrency, items, on_update, cancel):
        self.runs.append(SimpleNamespace(arl=arl, quality=quality, concurrency=concurrency, items=items))
        out = []
        for i, item in enumerate(items):
            if self.gate and i == 1:
                self.gate.wait(5)
            if cancel.is_set():
                u = ItemUpdate(i, "cancelled")
            else:
                on_update(ItemUpdate(i, "downloading", 50.0))
                os.makedirs(item.output_dir, exist_ok=True)
                path = os.path.join(item.output_dir, f"{item.track_number or 0:02d} - {item.deezer_id}.mp3")
                with open(path, "w") as f:
                    f.write(str(item.deezer_id))
                u = ItemUpdate(i, "done", 100.0, path, "MP3_128")
            on_update(u)
            out.append(u)
        return out


def make(tmp_path, adapter=None, coll=None, resolver=None, default_arl=ARL, **kw):
    settings = Settings(data_dir=tmp_path / "data", secret_key="k", default_arl=default_arl, **kw)
    users = UserStore(settings.data_dir / "users", "k", settings.max_user_concurrency)
    return JobManager(settings, users, adapter or FakeAdapter(), resolver or FakeResolver(coll or collection()),
                      FakeMatcher(), MatchCache(settings.data_dir / "match_cache.json"))


def wait(jm, user, phases, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snap = jm.snapshot(user)
        if snap and snap["phase"] in phases:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"timeout; last snapshot: {jm.snapshot(user)}")


def ready(jm, user="ana"):
    jm.analyze(user, URL)
    return wait(jm, user, {"ready"})


def test_full_flow_to_zip(tmp_path):
    (tmp_path / "data" / "jobs" / "stale").mkdir(parents=True)
    jm = make(tmp_path)  # startup wipes old jobs
    assert not (tmp_path / "data" / "jobs" / "stale").exists()

    job_id = jm.analyze("ana", URL)
    snap = wait(jm, "ana", {"ready"})
    assert jm.resolver.calls == ["spotify:album:4aawyAB9vmqN3uQ7FjRGTy"]
    assert snap["job_id"] == job_id
    assert snap["collection"]["track_count"] == 3
    assert snap["analysis"] == {"done": 3, "total": 3}
    assert snap["account"] is None and snap["download_url"] is None
    assert [i["match"]["status"] for i in snap["items"]] == ["MATCHED", "AMBIGUOUS", "NOT_FOUND"]
    assert snap["items"][1]["match"]["alternatives"][1] == {"deezer": {
        "id": 201, "title": "T201", "artist": "Art", "album": "Alb", "duration_s": 200}, "score": 0.65}
    assert snap["totals"]["matched"] == 1 and snap["totals"]["ambiguous"] == 1

    jm.choose("ana", 1, 201)
    snap = jm.snapshot("ana")
    assert snap["items"][1]["match"]["status"] == "MATCHED"
    assert snap["items"][1]["match"]["method"] == "manual"
    assert jm.cache.get("sp1").track.id == 201

    jm.download("ana")
    snap = wait(jm, "ana", {"done", "error"})
    assert snap["phase"] == "done", snap["error"]
    assert snap["account"] == {"max_quality": "MP3_128", "using_own_arl": False}
    assert snap["download_url"] == "/api/job/file"
    assert [i["download"]["state"] for i in snap["items"]] == ["done", "done", "pending"]
    assert snap["totals"]["done"] == 2
    run = jm.adapter.runs[0]
    assert (run.arl, run.quality, run.concurrency) == (ARL, "FLAC", 3)

    path = jm.file_for("ana")
    assert path.name == "Alb.zip"
    assert not (path.parent / "files").exists()  # loose tracks are dropped once zipped
    with zipfile.ZipFile(path) as z:
        assert sorted(z.namelist()) == ["01 - 100.mp3", "02 - 201.mp3"]
        assert z.getinfo("01 - 100.mp3").compress_type == zipfile.ZIP_STORED


def test_single_track_served_directly(tmp_path):
    jm = make(tmp_path, coll=collection("track", 1))
    ready(jm)
    jm.download("ana")
    wait(jm, "ana", {"done"})
    assert jm.file_for("ana").suffix == ".mp3"


def test_playlist_zip_includes_m3u8(tmp_path):
    jm = make(tmp_path, coll=collection("playlist", 2, title="Mix"))
    ready(jm)
    jm.choose("ana", 1, 101)
    jm.download("ana")
    wait(jm, "ana", {"done"})
    with zipfile.ZipFile(jm.file_for("ana")) as z:
        assert "Mix.m3u8" in z.namelist()


def test_users_are_isolated(tmp_path):
    jm = make(tmp_path)
    ready(jm)
    assert jm.snapshot("bob") is None
    assert jm.file_for("bob") is None
    for action in (lambda: jm.choose("bob", 0, 100), lambda: jm.download("bob"), lambda: jm.cancel("bob")):
        with pytest.raises(NoJob):
            action()
    assert jm.snapshot("ana")["phase"] == "ready"


def test_busy_while_downloading_and_replace_otherwise(tmp_path):
    gate = threading.Event()
    jm = make(tmp_path, adapter=FakeAdapter(gate=gate))
    first = jm.analyze("ana", URL)
    wait(jm, "ana", {"ready"})
    second = jm.analyze("ana", URL)  # ready -> replaced
    assert second != first
    wait(jm, "ana", {"ready"})
    jm.download("ana")
    wait(jm, "ana", {"downloading"})
    with pytest.raises(Busy):
        jm.analyze("ana", URL)
    jm.analyze("bob", URL)  # other users are unaffected
    gate.set()
    wait(jm, "ana", {"done"})
    assert jm.snapshot("ana")["job_id"] == second


def test_replacing_deletes_old_job_folder(tmp_path):
    jm = make(tmp_path)
    ready(jm)
    jm.download("ana")
    old = wait(jm, "ana", {"done"})["job_id"]
    assert (tmp_path / "data" / "jobs" / old).exists()
    ready(jm)
    assert not (tmp_path / "data" / "jobs" / old).exists()


def test_no_arl(tmp_path):
    jm = make(tmp_path, default_arl=None)
    ready(jm)
    with pytest.raises(NoArl):
        jm.download("ana")
    assert jm.snapshot("ana")["phase"] == "ready"


def test_own_arl_is_used(tmp_path):
    jm = make(tmp_path)
    jm.users.set_arl("ana", OWN_ARL)
    jm.users.update("ana", quality="MP3_320")
    ready(jm)
    jm.download("ana")
    snap = wait(jm, "ana", {"done"})
    assert snap["account"]["using_own_arl"] is True
    run = jm.adapter.runs[0]
    assert (run.arl, run.quality, run.concurrency) == (OWN_ARL, "MP3_320", 3)


def test_concurrency_comes_from_server_not_user(tmp_path):
    jm = make(tmp_path, max_user_concurrency=2)
    jm.users.update("ana", concurrency=1)
    ready(jm)
    jm.download("ana")
    wait(jm, "ana", {"done"})
    assert jm.adapter.runs[0].concurrency == 2


def test_choose_validation(tmp_path):
    jm = make(tmp_path)
    with pytest.raises(NoJob):
        jm.choose("ana", 0, 1)
    ready(jm)
    with pytest.raises(ValueError):
        jm.choose("ana", 1, 999)  # not among the options
    with pytest.raises(ValueError):
        jm.choose("ana", 2, 100)  # NOT_FOUND has no options
    with pytest.raises(ValueError):
        jm.choose("ana", 7, None)
    assert jm.cache.get("sp1") is None


def test_skip_is_respected(tmp_path):
    jm = make(tmp_path)
    ready(jm)
    jm.choose("ana", 1, 101)
    jm.choose("ana", 0, None)
    snap = jm.snapshot("ana")
    assert snap["items"][0]["skipped"] is True
    assert snap["totals"]["skipped"] == 1
    jm.download("ana")
    wait(jm, "ana", {"done"})
    assert [it.deezer_id for it in jm.adapter.runs[0].items] == [101]
    assert jm.file_for("ana").suffix == ".mp3"


def test_nothing_to_download(tmp_path):
    jm = make(tmp_path)
    ready(jm)
    jm.choose("ana", 0, None)  # 1 is ambiguous (not chosen), 2 not found
    with pytest.raises(ValueError):
        jm.download("ana")
    assert jm.snapshot("ana")["phase"] == "ready"


def test_cancel_midway(tmp_path):
    gate = threading.Event()
    jm = make(tmp_path, adapter=FakeAdapter(gate=gate))
    ready(jm)
    jm.choose("ana", 1, 101)
    jm.download("ana")
    wait(jm, "ana", {"downloading"})
    jm.cancel("ana")
    gate.set()
    snap = wait(jm, "ana", {"cancelled", "done", "error"})
    assert snap["phase"] == "cancelled"
    assert snap["download_url"] is None
    assert jm.file_for("ana") is None
    assert not list((tmp_path / "data" / "jobs").rglob("*.mp3"))
    with pytest.raises(WrongPhase):
        jm.download("ana")


def test_discard_forgets_job_and_files(tmp_path):
    gate = threading.Event()
    jm = make(tmp_path, adapter=FakeAdapter(gate=gate))
    jm.discard("ana")  # nothing to forget: fine
    ready(jm)
    jm.download("ana")
    wait(jm, "ana", {"downloading"})
    with pytest.raises(Busy):
        jm.discard("ana")
    gate.set()
    wait(jm, "ana", {"done"})
    jm.discard("ana")
    assert jm.snapshot("ana") is None and jm.file_for("ana") is None
    assert not list((tmp_path / "data" / "jobs").iterdir())


def test_cancel_during_ready(tmp_path):
    jm = make(tmp_path)
    ready(jm)
    jm.cancel("ana")
    assert jm.snapshot("ana")["phase"] == "cancelled"


def test_wrong_phase(tmp_path):
    jm = make(tmp_path)
    ready(jm)
    jm.download("ana")
    wait(jm, "ana", {"done"})
    with pytest.raises(WrongPhase):
        jm.download("ana")
    with pytest.raises(WrongPhase):
        jm.choose("ana", 1, 101)


def test_ttl_cleanup(tmp_path):
    jm = make(tmp_path, job_ttl_hours=1)
    ready(jm)
    jm.download("ana")
    job_dir = tmp_path / "data" / "jobs" / wait(jm, "ana", {"done"})["job_id"]
    assert job_dir.exists()

    jm.cleanup_expired(now=time.time() + 1800)
    assert jm.snapshot("ana") is not None
    jm.cleanup_expired(now=time.time() + 3601)
    assert jm.snapshot("ana") is None
    assert not job_dir.exists()


def test_ttl_spares_running_downloads(tmp_path):
    gate = threading.Event()
    jm = make(tmp_path, adapter=FakeAdapter(gate=gate), job_ttl_hours=1)
    ready(jm)
    jm.download("ana")
    wait(jm, "ana", {"downloading"})
    jm.cleanup_expired(now=time.time() + 7200)
    assert jm.snapshot("ana")["phase"] == "downloading"
    gate.set()
    wait(jm, "ana", {"done"})


def test_disk_full_refuses(tmp_path, monkeypatch):
    jm = make(tmp_path, min_free_gb=2)
    ready(jm)
    monkeypatch.setattr(jobs_mod.shutil, "disk_usage", lambda p: SimpleNamespace(total=0, used=0, free=1e9))
    with pytest.raises(DiskFull):
        jm.download("ana")
    assert jm.snapshot("ana")["phase"] == "ready"


def test_auth_error_becomes_error_phase_without_arl(tmp_path, caplog):
    jm = make(tmp_path, adapter=FakeAdapter(auth_error=True))
    ready(jm)
    jm.download("ana")
    snap = wait(jm, "ana", {"error"})
    assert "ARL inválido ou expirado" in snap["error"]
    assert "padrão" in snap["error"]
    assert ARL not in str(snap)
    assert ARL not in caplog.text


def test_done_is_recorded_in_history(tmp_path):
    jm = make(tmp_path)
    ready(jm)
    assert jm.history.recent() == []  # analysing is not downloading
    jm.download("ana")
    wait(jm, "ana", {"done"})
    (entry,) = jm.history.recent()
    assert (entry["user"], entry["kind"], entry["title"]) == ("ana", "album", "Alb")
    assert entry["url"] == "https://open.spotify.com/album/x"
    assert (tmp_path / "data" / "history.json").exists()


def test_cancelled_and_failed_are_not_recorded(tmp_path):
    gate = threading.Event()
    jm = make(tmp_path, adapter=FakeAdapter(gate=gate))
    ready(jm)
    jm.choose("ana", 1, 101)
    jm.download("ana")
    wait(jm, "ana", {"downloading"})
    jm.cancel("ana")
    gate.set()
    wait(jm, "ana", {"cancelled"})
    assert jm.history.recent() == []

    jm = make(tmp_path, adapter=FakeAdapter(auth_error=True))
    ready(jm)
    jm.download("ana")
    wait(jm, "ana", {"error"})
    assert jm.history.recent() == []


def test_history_failure_does_not_break_the_job(tmp_path, monkeypatch):
    jm = make(tmp_path)

    def boom(user, collection, quality=None):
        raise OSError("disk went away")
    monkeypatch.setattr(jm.history, "add", boom)
    ready(jm)
    jm.download("ana")
    assert wait(jm, "ana", {"done", "error"})["phase"] == "done"
    assert jm.file_for("ana") is not None


def test_spotify_errors(tmp_path):
    jm = make(tmp_path, resolver=FakeResolver(error=SpotifyError("Playlist is private.")))
    with pytest.raises(SpotifyError):
        jm.analyze("ana", "https://example.com/x")  # parse_url fails synchronously
    assert jm.snapshot("ana") is None
    jm.analyze("ana", URL)
    assert wait(jm, "ana", {"error"})["error"] == "Playlist is private."
