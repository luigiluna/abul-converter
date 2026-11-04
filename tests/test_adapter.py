import inspect
import threading
import time
from pathlib import Path

import pytest

from app.backend.downloads import adapter as adapter_mod
from app.backend.downloads.adapter import AuthError, DeezloadAdapter
from app.backend.downloads.layout import plan_downloads, write_m3u8
from app.backend.models import (DeezerTrack, DownloadItem, MatchResult, MatchStatus,
                                SpotifyCollection, TrackCandidate)


def test_deezload_contract_still_holds():
    """Fails loudly if an upstream Deezload update changes what we rely on."""
    dz = adapter_mod.deezload
    params = inspect.signature(dz.DeezerDownloader.download_track).parameters
    assert list(params)[:6] == ["self", "track_id", "output_dir", "track_num", "disc_num", "progress_cb"]
    assert {"arl_token", "quality", "max_retries", "fetch_lyrics"} <= set(
        inspect.signature(dz.DeezerDownloader.__init__).parameters)
    assert set(adapter_mod.QUALITIES) == {"FLAC", "MP3_320", "MP3_128"}
    assert dz.log is adapter_mod._deezload_log


class FakeDownloader:
    """Mimics DeezerDownloader: returns a path or None, calls progress_cb."""

    instances = []

    def __init__(self, arl, quality, max_retries, fetch_lyrics):
        self.arl, self.quality = arl, quality
        paid = arl != "free"
        self.client = type("Client", (), {"current_user": {
            "name": "Tester", "can_stream_hq": paid, "can_stream_lossless": paid}})()
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        FakeDownloader.instances.append(self)

    def authenticate(self):
        if self.arl == "bad":
            adapter_mod.deezload.log("✗ Authentication failed. Check your ARL token.", "error")
            return False
        return True

    def download_track(self, track_id, output_dir, track_num=None, disc_num=None, progress_cb=None):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if track_id == "404":
                adapter_mod.deezload.log(" ✗ Could not get track info for 404", "error")
                return None
            for pct in range(0, 101, 10):
                try:
                    progress_cb(pct)
                except Exception:  # Deezload breaks out of its retry loop and returns None
                    return None
                time.sleep(0.01 if track_id != "slow" else 0.2)
            ext = ".mp3" if track_id == "noflac" or self.quality.value != "FLAC" else ".flac"
            return str(Path(output_dir) / f"{track_id}{ext}")
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def fake(monkeypatch):
    FakeDownloader.instances.clear()
    monkeypatch.setattr(adapter_mod.deezload, "DeezerDownloader", FakeDownloader)
    return FakeDownloader


def run(adapter, ids, quality="FLAC", concurrency=2, cancel=None, arl="good"):
    updates = []
    # ids are strings so the fake can branch on them ("404", "slow", ...)
    its = [DownloadItem(deezer_id=i, output_dir="out") for i in ids]
    out = adapter.run(arl, quality, concurrency, its, updates.append, cancel or threading.Event())
    return out, updates


def test_success_failure_and_fallback(fake):
    out, updates = run(DeezloadAdapter(), ["1", "404", "noflac"])
    assert [u.state for u in out] == ["done", "failed", "done"]
    assert out[0].path.endswith("1.flac") and out[0].quality == "FLAC"
    assert not out[0].fallback_quality
    assert out[1].error == "Could not get track info for 404"
    assert out[2].fallback_quality and out[2].quality.startswith("MP3")
    assert {u.state for u in updates} >= {"queued", "downloading", "done", "failed"}


def test_bad_arl_raises_auth_error(fake):
    with pytest.raises(AuthError, match="Authentication failed"):
        run(DeezloadAdapter(), ["1"], arl="bad")


def test_downloaders_are_pooled_per_arl_and_quality(fake):
    a = DeezloadAdapter()
    run(a, ["1"], "FLAC")
    run(a, ["1"], "FLAC")
    run(a, ["1"], "MP3_320")
    run(a, ["1"], "FLAC", arl="other")
    # One MP3_128 instance per ARL is the login used for the plan check.
    assert sorted((d.arl, d.quality.value) for d in fake.instances) == [
        ("good", "FLAC"), ("good", "MP3_128"), ("good", "MP3_320"),
        ("other", "FLAC"), ("other", "MP3_128")]


def test_free_account_is_capped_to_its_plan(fake):
    a = DeezloadAdapter()
    assert a.account_info("free").max_quality == "MP3_128"
    assert a.account_info("good").max_quality == "FLAC"
    out, _ = run(a, ["1"], "FLAC", arl="free")
    assert out[0].quality == "MP3" and out[0].fallback_quality  # fake file is not a real MP3
    assert {d.quality.value for d in fake.instances if d.arl == "free"} == {"MP3_128"}


def test_global_limit_is_shared_between_runs(fake):
    a = DeezloadAdapter(max_parallel=2)
    threads = [threading.Thread(target=run, args=(a, ["1", "2", "3"], "FLAC", 3)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    flac = [d for d in fake.instances if d.quality.value == "FLAC"]
    assert len(flac) == 1 and flac[0].max_active <= 2


def test_cancel_mid_download(fake):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    out, _ = run(DeezloadAdapter(), ["slow", "slow", "slow", "slow"], concurrency=1, cancel=cancel)
    states = [u.state for u in out]
    assert "cancelled" in states and "done" not in states[1:]


def test_unknown_quality(fake):
    with pytest.raises(ValueError):
        run(DeezloadAdapter(), ["1"], quality="WAV")


# --- layout ----------------------------------------------------------------

def _collection(kind, n=2):
    tracks = tuple(TrackCandidate(f"s{i}", f"T{i}", ("A",), "Alb" if kind == "album" else None,
                                  1000, track_number=i if kind == "album" else None)
                   for i in range(1, n + 1))
    return SpotifyCollection(kind, "id", "My: List" if kind == "playlist" else "Alb", "A", None, tracks)


def _results(n=2):
    rs = [MatchResult(MatchStatus.MATCHED, DeezerTrack(i, "t", "a", "al", 1)) for i in range(1, n + 1)]
    rs[-1] = MatchResult(MatchStatus.AMBIGUOUS, DeezerTrack(99, "t", "a", "al", 1))
    return rs


def test_plan_album(tmp_path):
    planned = plan_downloads(_collection("album"), _results(), str(tmp_path))
    assert [(i, it.deezer_id, it.track_number) for i, it in planned] == [(0, 1, 1)]
    assert planned[0][1].output_dir == str(tmp_path / "A - Alb")


def test_plan_playlist_and_m3u8(tmp_path):
    col = _collection("playlist", 3)
    planned = plan_downloads(col, _results(3), str(tmp_path))
    folder = Path(planned[0][1].output_dir)
    assert folder == tmp_path / "My- List"
    assert all(it.track_number is None for _, it in planned)
    folder.mkdir()
    m3u = write_m3u8(col, [str(folder / "a.flac"), None, str(folder / "b.mp3")])
    assert Path(m3u).read_text(encoding="utf-8").splitlines() == ["#EXTM3U", "a.flac", "b.mp3"]


def test_actual_quality_reads_mp3_bitrate(monkeypatch):
    class Info:
        bitrate = 320_000

    class FakeMP3:
        def __init__(self, path):
            self.info = Info()

    import mutagen.mp3
    monkeypatch.setattr(mutagen.mp3, "MP3", FakeMP3)
    assert adapter_mod.actual_quality("a.flac") == "FLAC"
    assert adapter_mod.actual_quality("a.mp3") == "MP3_320"
    Info.bitrate = 128_000
    assert adapter_mod.actual_quality("a.mp3") == "MP3_128"


def test_idle_login_is_redone(fake):
    now = [0.0]
    a = DeezloadAdapter(clock=lambda: now[0])
    run(a, ["1"], "FLAC")
    now[0] += adapter_mod.SESSION_IDLE_SECONDS - 1  # still fresh, and this use resets the timer
    run(a, ["1"], "FLAC")
    now[0] += adapter_mod.SESSION_IDLE_SECONDS - 1
    run(a, ["1"], "FLAC")
    assert len(fake.instances) == 2  # one FLAC + one MP3_128 (plan check), reused
    now[0] += adapter_mod.SESSION_IDLE_SECONDS + 1  # idle too long: log in again
    out, _ = run(a, ["1"], "FLAC")
    assert out[0].state == "done"
    assert sorted(d.quality.value for d in fake.instances) == ["FLAC", "FLAC", "MP3_128", "MP3_128"]
