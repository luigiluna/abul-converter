import json
import logging

import pytest

from app.backend import secrets_filter
from app.backend.users import UserPrefs, UserStore

ARL = "a1b2c3d4e5f6" * 8


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users", "secret-key", max_concurrency=3)


def test_defaults_for_unknown_user(store):
    assert store.get("ana") == UserPrefs()
    assert not store.has_own_arl("ana")


def test_update_persists_and_validates(store, tmp_path):
    assert store.update("ana", quality="MP3_320", concurrency=3) == UserPrefs("MP3_320", 3)
    assert UserStore(tmp_path / "users", "secret-key").get("ana") == UserPrefs("MP3_320", 3)
    assert store.update("ana", concurrency=1).quality == "MP3_320"

    for bad in [dict(quality="WAV"), dict(concurrency=0), dict(concurrency=4), dict(concurrency=True)]:
        with pytest.raises(ValueError):
            store.update("ana", **bad)
    assert store.get("ana") == UserPrefs("MP3_320", 1)


@pytest.mark.parametrize("name", ["../x", "Ana", "", "a" * 33, "a b", "a.json", "ana\n", "..", "a/b"])
def test_invalid_username(store, name):
    with pytest.raises(ValueError):
        store.get(name)
    with pytest.raises(ValueError):
        store.set_arl(name, ARL)


def test_arl_is_encrypted_on_disk(store, tmp_path):
    store.set_arl("ana", ARL)
    raw = (tmp_path / "users" / "ana.json").read_text(encoding="utf-8")
    assert ARL not in raw
    assert set(json.loads(raw)) == {"arl_enc"}
    assert store.has_own_arl("ana")


def test_arl_for_own_default_and_removal(store):
    assert store.arl_for("ana", "default-arl") == "default-arl"
    assert store.arl_for("ana", None) is None
    store.set_arl("ana", ARL)
    assert store.arl_for("ana", "default-arl") == ARL
    assert store.arl_for("bob", "default-arl") == "default-arl"  # only for that user
    store.set_arl("ana", None)
    assert store.arl_for("ana", "default-arl") == "default-arl"
    assert not store.has_own_arl("ana")
    store.set_arl("ana", ARL)
    store.set_arl("ana", "  ")
    assert not store.has_own_arl("ana")


def test_wrong_key_falls_back_to_default(store, tmp_path, caplog):
    store.set_arl("ana", ARL)
    other = UserStore(tmp_path / "users", "another-key")
    with caplog.at_level(logging.WARNING):
        assert other.arl_for("ana", "default-arl") == "default-arl"
    assert not other.has_own_arl("ana")
    assert "could not decrypt" in caplog.text
    assert ARL not in caplog.text


def test_corrupt_user_file_uses_defaults(store, tmp_path):
    (tmp_path / "users").mkdir()
    (tmp_path / "users" / "ana.json").write_text("{nope", encoding="utf-8")
    assert store.get("ana") == UserPrefs()
    assert store.arl_for("ana", "d") == "d"


# --- secrets_filter ----------------------------------------------------------

def test_filter_masks_secret_in_formatted_log(caplog):
    f = secrets_filter.install(lambda: [ARL, "", "short"])
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("app.some.child").info("login with %s failed", ARL)
            logging.getLogger("app.x").info("prefix-%s-suffix and short stays", ARL)
            try:
                raise RuntimeError(f"bad arl {ARL}")
            except RuntimeError:
                logging.getLogger("app.y").exception("boom")
    finally:
        secrets_filter.uninstall(f)

    assert ARL not in caplog.text
    assert "login with *** failed" in caplog.text
    assert "prefix-***-suffix and short stays" in caplog.text
    assert "bad arl ***" in caplog.text


def test_filter_reads_current_secrets(caplog):
    secrets = []
    f = secrets_filter.install(lambda: secrets)
    try:
        with caplog.at_level(logging.INFO):
            secrets.append(ARL)
            logging.getLogger("app").info("arl=%s", ARL)
    finally:
        secrets_filter.uninstall(f)
    assert ARL not in caplog.text
