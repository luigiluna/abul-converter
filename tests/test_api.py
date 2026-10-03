import io
import json
import logging
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.backend import api as api_mod
from app.backend import jobs as jobs_mod
from app.backend import main as main_mod
from app.backend.api import create_app
from app.backend.jobs import JobManager
from app.backend.matching.cache import MatchCache
from app.backend.settings import ConfigError, Settings
from app.backend.users import UserStore
from tests.test_jobs import ARL, OWN_ARL, URL, FakeAdapter, FakeMatcher, FakeResolver, collection

CSRF = {"X-Requested-With": "spot2deez"}


def ana(**extra):
    return {"X-Remote-User": "ana", **CSRF, **extra}


def bia(**extra):
    return {"X-Remote-User": "bia", **CSRF, **extra}


@pytest.fixture
def make(tmp_path):
    clients = []

    def _make(adapter=None, resolver=None, default_arl=ARL, frontend_dir=None,
              raise_server_exceptions=True, **kw):
        settings = Settings(data_dir=tmp_path / "data", secret_key="k", default_arl=default_arl, **kw)
        users = UserStore(settings.data_dir / "users", "k", settings.max_user_concurrency)
        jm = JobManager(settings, users, adapter or FakeAdapter(), resolver or FakeResolver(collection()),
                        FakeMatcher(), MatchCache(settings.data_dir / "match_cache.json"))
        app = create_app(settings, jobs=jm, frontend_dir=frontend_dir or tmp_path / "no-frontend")
        client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        client.__enter__()  # runs the lifespan (filter install + cleanup thread)
        clients.append(client)
        client.jm = jm
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def poll(client, phases, headers=None, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = client.get("/api/job", headers=headers or ana())
        if r.status_code == 200 and r.json()["phase"] in phases:
            return r.json()
        time.sleep(0.01)
    raise AssertionError(f"timeout; last: {r.status_code} {r.text}")


def ready(client, headers=None):
    r = client.post("/api/analyze", json={"url": URL}, headers=headers or ana())
    assert r.status_code == 202, r.text
    return poll(client, {"ready"}, headers)


# --- identity + CSRF ----------------------------------------------------------

def test_missing_user_is_401_in_prod(make):
    c = make()
    r = c.get("/api/me")
    assert r.status_code == 401 and "error" in r.json()


def test_dev_user_ignored_in_prod(make):
    c = make(app_env="prod", dev_user="eu")
    assert c.get("/api/me").status_code == 401


def test_dev_user_in_dev(make):
    c = make(app_env="dev", dev_user="eu")
    r = c.get("/api/me")
    assert r.status_code == 200 and r.json()["user"] == "eu"
    # an explicit header still wins
    assert c.get("/api/me", headers={"X-Remote-User": "ana"}).json()["user"] == "ana"


def test_username_lowercased_and_validated(make):
    c = make()
    assert c.get("/api/me", headers={"X-Remote-User": "Ana"}).json()["user"] == "ana"
    for bad in ("../x", "a b", "x" * 33, "é"):
        r = c.get("/api/me", headers={"X-Remote-User": bad.encode("utf-8")})
        assert r.status_code == 400, bad


def test_csrf_header_required(make):
    c = make()
    h = {"X-Remote-User": "ana"}
    assert c.post("/api/analyze", json={"url": URL}, headers=h).status_code == 403
    assert c.put("/api/me", json={"quality": "MP3_128"}, headers=h).status_code == 403
    assert c.post("/api/job/cancel", json={}, headers={**h, "X-Requested-With": "XMLHttpRequest"}).status_code == 403
    assert c.delete("/api/me", headers=h).status_code == 403
    # GETs do not need it
    assert c.get("/api/me", headers=h).status_code == 200


def test_no_cors(make):
    c = make()
    r = c.options("/api/analyze", headers={"Origin": "https://evil.example",
                                           "Access-Control-Request-Method": "POST", **ana()})
    assert "access-control-allow-origin" not in r.headers


# --- /api/me ------------------------------------------------------------------

def test_me_defaults(make):
    c = make()
    assert c.get("/api/me", headers=ana()).json() == {
        "user": "ana", "quality": "FLAC", "concurrency": 2, "own_arl": False,
        "default_arl_configured": True, "max_concurrency": 3}


def test_put_me_never_returns_arl(make, tmp_path):
    c = make()
    r = c.put("/api/me", json={"quality": "MP3_320", "concurrency": 3, "arl": OWN_ARL}, headers=ana())
    assert r.status_code == 200
    assert r.json()["own_arl"] is True and r.json()["quality"] == "MP3_320" and r.json()["concurrency"] == 3
    assert OWN_ARL not in r.text and ARL not in r.text
    r = c.get("/api/me", headers=ana())
    assert OWN_ARL not in r.text and ARL not in r.text and r.json()["own_arl"] is True
    assert OWN_ARL not in (tmp_path / "data" / "users" / "ana.json").read_text()
    assert c.jm.users.arl_for("ana", ARL) == OWN_ARL

    r = c.put("/api/me", json={"arl": None}, headers=ana())
    assert r.json()["own_arl"] is False and r.json()["quality"] == "MP3_320"


def test_put_me_invalid(make):
    c = make()
    for body in ({"quality": "WAV"}, {"concurrency": 9}, {"concurrency": "2"}, {"concurrency": True},
                 {"arl": 123}):
        r = c.put("/api/me", json=body, headers=ana())
        assert r.status_code == 400 and r.json()["error"], body
    assert c.put("/api/me", content="nope", headers=ana()).status_code == 400
    assert c.put("/api/me", json=[1], headers=ana()).status_code == 400
    assert c.get("/api/me", headers=ana()).json()["quality"] == "FLAC"


def test_received_arl_is_masked_in_logs(make, caplog):
    c = make()
    c.put("/api/me", json={"arl": OWN_ARL}, headers=ana())
    with caplog.at_level(logging.INFO):
        logging.getLogger("spot2deez.test").warning("own=%s default=%s", OWN_ARL, ARL)
    assert OWN_ARL not in caplog.text and ARL not in caplog.text
    assert "***" in caplog.text


# --- job flow -----------------------------------------------------------------

def test_no_job_is_204(make):
    c = make()
    r = c.get("/api/job", headers=ana())
    assert r.status_code == 204 and r.content == b""


def test_full_flow_to_file(make):
    c = make()
    r = c.post("/api/analyze", json={"url": URL}, headers=ana())
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    snap = poll(c, {"ready"})
    assert snap["job_id"] == job_id
    assert [i["match"]["status"] for i in snap["items"]] == ["MATCHED", "AMBIGUOUS", "NOT_FOUND"]

    r = c.post("/api/job/items/1/choose", json={"deezer_id": 201}, headers=ana())
    assert r.status_code == 200
    assert r.json()["items"][1]["match"]["status"] == "MATCHED"
    assert r.json()["items"][1]["match"]["deezer"]["id"] == 201

    r = c.post("/api/job/download", json={}, headers=ana())
    assert r.status_code == 200 and r.json()["phase"] in ("downloading", "packaging", "done")
    snap = poll(c, {"done"})
    assert snap["download_url"] == "/api/job/file"
    assert snap["account"] == {"max_quality": "MP3_128", "using_own_arl": False}
    assert ARL not in json.dumps(snap)

    r = c.get(snap["download_url"], headers={"X-Remote-User": "ana"})
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert ".zip" in r.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(n for n in names if n.endswith(".mp3")) == ["01 - 100.mp3", "02 - 201.mp3"]


def test_skip_via_null(make):
    c = make()
    ready(c)
    r = c.post("/api/job/items/0/choose", json={"deezer_id": None}, headers=ana())
    assert r.status_code == 200 and r.json()["items"][0]["skipped"] is True


def test_other_user_cannot_see_job(make):
    c = make()
    ready(c)
    c.post("/api/job/download", json={}, headers=ana())
    poll(c, {"done"})
    assert c.get("/api/job", headers=bia()).status_code == 204
    assert c.get("/api/job/file", headers=bia()).status_code == 404
    for path, body in (("/api/job/items/1/choose", {"deezer_id": 101}), ("/api/job/download", {}),
                       ("/api/job/cancel", {})):
        r = c.post(path, json=body, headers=bia())
        assert r.status_code == 404 and r.json()["error"], path
    assert c.get("/api/job/file", headers={"X-Remote-User": "ana"}).status_code == 200


def test_file_404_before_done(make):
    c = make()
    assert c.get("/api/job/file", headers=ana()).status_code == 404
    ready(c)
    assert c.get("/api/job/file", headers=ana()).status_code == 404


def test_choose_errors(make):
    c = make()
    assert c.post("/api/job/items/0/choose", json={"deezer_id": 1}, headers=ana()).status_code == 404
    ready(c)
    for path, body in (("/api/job/items/1/choose", {"deezer_id": 999}),
                       ("/api/job/items/9/choose", {"deezer_id": None}),
                       ("/api/job/items/-1/choose", {"deezer_id": None}),
                       ("/api/job/items/x/choose", {"deezer_id": 101}),
                       ("/api/job/items/1/choose", {}),
                       ("/api/job/items/1/choose", {"deezer_id": "101"}),
                       ("/api/job/items/1/choose", {"deezer_id": True})):
        r = c.post(path, json=body, headers=ana())
        assert r.status_code == 400 and r.json()["error"], (path, body)


def test_wrong_phase_is_409(make):
    c = make()
    ready(c)
    c.post("/api/job/download", json={}, headers=ana())
    poll(c, {"done"})
    assert c.post("/api/job/download", json={}, headers=ana()).status_code == 409
    assert c.post("/api/job/items/1/choose", json={"deezer_id": 101}, headers=ana()).status_code == 409


def test_busy_is_409(make):
    gate = threading.Event()
    c = make(adapter=FakeAdapter(gate=gate))
    ready(c)
    c.post("/api/job/items/1/choose", json={"deezer_id": 101}, headers=ana())
    c.post("/api/job/download", json={}, headers=ana())
    r = c.post("/api/analyze", json={"url": URL}, headers=ana())
    assert r.status_code == 409 and r.json()["error"]
    r = c.post("/api/job/cancel", json={}, headers=ana())
    assert r.status_code == 200
    gate.set()
    assert poll(c, {"cancelled", "done", "error"})["phase"] == "cancelled"


def test_spotify_error_is_400(make):
    c = make()
    r = c.post("/api/analyze", json={"url": "https://example.com/x"}, headers=ana())
    assert r.status_code == 400 and r.json()["error"]
    for body in ({}, {"url": ""}, {"url": 5}):
        assert c.post("/api/analyze", json=body, headers=ana()).status_code == 400


def test_no_arl_is_400(make):
    c = make(default_arl=None)
    assert c.get("/api/me", headers=ana()).json()["default_arl_configured"] is False
    ready(c)
    r = c.post("/api/job/download", json={}, headers=ana())
    assert r.status_code == 400 and "ARL" in r.json()["error"]


def test_disk_full_is_400(make, monkeypatch):
    c = make(min_free_gb=2)
    ready(c)
    monkeypatch.setattr(jobs_mod.shutil, "disk_usage", lambda p: SimpleNamespace(total=0, used=0, free=1e9))
    r = c.post("/api/job/download", json={}, headers=ana())
    assert r.status_code == 400 and "espaço" in r.json()["error"]


def test_cancel_without_job_is_404(make):
    c = make()
    assert c.post("/api/job/cancel", json={}, headers=ana()).status_code == 404


def test_unexpected_error_is_500_without_secret(make, monkeypatch):
    c = make(raise_server_exceptions=False)

    def boom(user):
        raise RuntimeError(f"boom {ARL}")
    monkeypatch.setattr(c.jm, "snapshot", boom)
    r = c.get("/api/job", headers=ana())
    assert r.status_code == 500 and r.json() == {"error": "Erro interno."}


def test_unknown_api_route_is_json_404(make):
    c = make()
    r = c.get("/api/nope", headers=ana())
    assert r.status_code == 404 and "error" in r.json()


# --- frontend -----------------------------------------------------------------

def test_frontend_missing_does_not_break(make):
    c = make()
    r = c.get("/")
    assert r.status_code == 404 and "Frontend" in r.json()["error"]
    assert c.get("/app.js").status_code == 404
    assert c.get("/api/me", headers=ana()).status_code == 200


def test_frontend_served(make, tmp_path):
    fe = tmp_path / "fe"
    fe.mkdir()
    (fe / "index.html").write_text("<!doctype html><title>spot2deez</title>", encoding="utf-8")
    (fe / "app.js").write_text("console.log(1)", encoding="utf-8")
    c = make(frontend_dir=fe)
    r = c.get("/")
    assert r.status_code == 200 and "spot2deez" in r.text and r.headers["content-type"].startswith("text/html")
    assert c.get("/app.js").text == "console.log(1)"
    assert c.get("/missing.css").status_code == 404


def test_frontend_created_after_startup(make, tmp_path):
    fe = tmp_path / "late"
    c = make(frontend_dir=fe)
    assert c.get("/app.js").status_code == 404
    fe.mkdir()
    (fe / "index.html").write_text("hi", encoding="utf-8")
    (fe / "app.js").write_text("x", encoding="utf-8")
    assert c.get("/").text == "hi"
    assert c.get("/app.js").text == "x"


def test_api_docs_disabled(make):
    c = make()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


# --- startup ------------------------------------------------------------------

def test_config_error_stops_startup(monkeypatch, capsys):
    def bad():
        raise ConfigError("APP_SECRET_KEY is required when APP_ENV=prod")
    monkeypatch.setattr(main_mod, "get_settings", bad)
    monkeypatch.setattr(main_mod.uvicorn, "run", lambda *a, **k: pytest.fail("server started"))
    assert main_mod.main() == 2
    assert "APP_SECRET_KEY is required" in capsys.readouterr().err


def test_main_runs_uvicorn_on_host_port(monkeypatch, tmp_path):
    settings = Settings(data_dir=tmp_path / "data", secret_key="k", default_arl=None)
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "create_app", lambda s: ("app", s))
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9000")
    calls = []
    monkeypatch.setattr(main_mod.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    assert main_mod.main() == 0
    assert calls == [(("app", settings), {"host": "0.0.0.0", "port": 9000, "log_config": None})]


def test_create_app_builds_real_dependencies(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", secret_key="k", default_arl=None)
    app = create_app(settings, frontend_dir=tmp_path)
    jm = app.state.jobs
    assert isinstance(jm, JobManager)
    assert jm.cache.path == tmp_path / "data" / "match_cache.json"
    assert jm.users.users_dir == tmp_path / "data" / "users"
    assert type(jm.adapter).__name__ == "DeezloadAdapter"
    assert api_mod.FRONTEND_DIR == api_mod.ROOT / "app" / "frontend"
