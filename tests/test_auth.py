import json

import pytest
from Crypto.Protocol.KDF import bcrypt

from app import passwd
from app.backend import auth
from app.backend.auth import Accounts, Throttle
from app.backend.settings import Settings


@pytest.fixture
def accounts(tmp_path):
    return Accounts(tmp_path / "data" / "accounts.json", "k", cost=4)


def test_set_password_and_verify(accounts):
    accounts.set_password("ana", "senha-da-ana")
    assert accounts.users() == ["ana"]
    assert accounts.verify("ana", "senha-da-ana")
    assert not accounts.verify("ana", "senha-da-anA")
    assert not accounts.verify("bia", "senha-da-ana")
    assert not accounts.verify("../x", "senha-da-ana")
    assert "senha-da-ana" not in accounts.path.read_text()


def test_set_password_validates(accounts):
    for user, password in (("Ana", "senha-da-ana"), ("../x", "senha-da-ana"), ("ana", "curta"), ("ana", "x" * 73)):
        with pytest.raises(ValueError):
            accounts.set_password(user, password)
    assert accounts.users() == []


def test_verifies_hashes_made_by_caddy(accounts):
    # caddy hash-password / htpasswd write "$2b$"/"$2y$"; pycryptodome writes "$2a$"
    stored = bcrypt(b"senha-da-ana", 4).decode()
    accounts.path.parent.mkdir(parents=True)
    accounts.path.write_text(json.dumps({"ana": "$2b$" + stored[4:], "bia": "$2y$" + stored[4:]}))
    assert accounts.verify("ana", "senha-da-ana") and accounts.verify("bia", "senha-da-ana")
    assert not accounts.verify("ana", "outra-senha")


def test_session_roundtrip_expiry_and_revocation(accounts):
    accounts.set_password("ana", "senha-da-ana")
    token = accounts.new_session("ana", now=1000)
    assert accounts.session_user(token, now=1000 + auth.SESSION_SECONDS - 1) == "ana"
    assert accounts.session_user(token, now=1000 + auth.SESSION_SECONDS + 1) is None
    assert Accounts(accounts.path, "other-key", cost=4).session_user(token, now=1000) is None
    for bad in (None, "", "ana", "ana.x.y", token + "0", token.replace("ana", "bia")):
        assert accounts.session_user(bad, now=1000) is None
    assert accounts.remove("ana") and not accounts.remove("ana")
    assert accounts.session_user(token, now=1000) is None
    with pytest.raises(ValueError):
        accounts.new_session("ana")


def test_corrupted_file_means_nobody_logs_in(accounts):
    accounts.path.parent.mkdir(parents=True)
    accounts.path.write_text("{nope")
    assert accounts.users() == [] and not accounts.verify("ana", "senha-da-ana")
    accounts.path.write_text(json.dumps({"ana": 5, "bia": "plain"}))
    assert accounts.users() == []


def test_throttle_window():
    t = Throttle(limit=2, window=10)
    assert not t.blocked("a", now=0)
    t.fail("a", now=0)
    t.fail("a", now=1)
    assert t.blocked("a", now=2) and not t.blocked("b", now=2)
    assert not t.blocked("a", now=10.5)  # the first failure aged out


def test_passwd_cli(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path, secret_key="k", default_arl=None)
    monkeypatch.setattr(passwd, "get_settings", lambda: settings)
    typed = iter(["senha-da-ana", "senha-da-ana", "senha-da-ana", "diferente-dela"])
    monkeypatch.setattr(passwd.getpass, "getpass", lambda prompt: next(typed))
    assert passwd.main(["Ana"]) == 0
    assert Accounts(tmp_path / "accounts.json", "k").verify("ana", "senha-da-ana")
    assert passwd.main(["ana"]) == 1  # the two entries differ
    assert passwd.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "ana" in out and "senha-da-ana" not in out
    assert passwd.main(["ana", "--remove"]) == 0 and passwd.main(["ana", "--remove"]) == 1
