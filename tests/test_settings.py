import os

import pytest

from app.backend import settings


def test_dotenv_parsing_and_env_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nDEFAULT_ARL="abc123"\nOTHER = x=y\n\nBROKEN LINE\n', encoding="utf-8")
    monkeypatch.delenv("DEFAULT_ARL", raising=False)
    monkeypatch.delenv("OTHER", raising=False)
    monkeypatch.setenv("KEEP", "from-env")
    env.write_text(env.read_text() + "KEEP=from-file\n", encoding="utf-8")

    settings.load_dotenv(env)

    assert os.environ["DEFAULT_ARL"] == "abc123"
    assert os.environ["OTHER"] == "x=y"
    assert os.environ["KEEP"] == "from-env"


def test_missing_dotenv_is_fine(tmp_path):
    settings.load_dotenv(tmp_path / "nope.env")


ENV_VARS = ["APP_ENV", "APP_SECRET_KEY", "DEFAULT_ARL", "DEEZLOAD_ARL", "DATA_DIR", "DEV_USER",
            "MAX_PARALLEL_DOWNLOADS", "MAX_USER_CONCURRENCY", "JOB_TTL_HOURS", "MIN_FREE_GB"]


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """No real .env, no inherited variables, fresh get_settings cache."""
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    settings.get_settings.cache_clear()
    yield monkeypatch
    settings.get_settings.cache_clear()


def test_prod_requires_secret_key(clean_env):
    with pytest.raises(settings.ConfigError, match="APP_SECRET_KEY"):
        settings.get_settings()


def test_defaults(clean_env, tmp_path):
    clean_env.setenv("APP_SECRET_KEY", "k" * 64)
    s = settings.get_settings()
    assert s == settings.Settings(data_dir=tmp_path / "data", secret_key="k" * 64, default_arl=None)
    assert settings.get_settings() is s  # loaded once


def test_values_from_dotenv_and_env(clean_env, tmp_path):
    (tmp_path / ".env").write_text("APP_SECRET_KEY=from-file\nDEFAULT_ARL=arl-file\nMAX_USER_CONCURRENCY=5\n",
                                   encoding="utf-8")
    clean_env.setenv("DATA_DIR", str(tmp_path / "d"))
    clean_env.setenv("JOB_TTL_HOURS", "0.5")
    clean_env.setenv("MAX_PARALLEL_DOWNLOADS", "2")
    s = settings.get_settings()
    assert (s.secret_key, s.default_arl, s.max_user_concurrency) == ("from-file", "arl-file", 5)
    assert (s.data_dir, s.job_ttl_hours, s.max_parallel_downloads) == (tmp_path / "d", 0.5, 2)


def test_dev_mode(clean_env):
    clean_env.setenv("APP_ENV", "dev")
    clean_env.setenv("DEV_USER", "luis")
    s = settings.get_settings()
    assert s.app_env == "dev" and s.dev_user == "luis"
    assert s.secret_key == settings.DEV_SECRET_KEY


def test_dev_user_ignored_in_prod(clean_env):
    clean_env.setenv("APP_SECRET_KEY", "k")
    clean_env.setenv("DEV_USER", "luis")
    assert settings.get_settings().dev_user is None


@pytest.mark.parametrize("name,value,match", [
    ("APP_ENV", "staging", "APP_ENV"),
    ("MAX_PARALLEL_DOWNLOADS", "many", "MAX_PARALLEL_DOWNLOADS"),
    ("MAX_USER_CONCURRENCY", "0", "MAX_USER_CONCURRENCY"),
    ("MIN_FREE_GB", "-1", "MIN_FREE_GB"),
])
def test_invalid_values(clean_env, name, value, match):
    clean_env.setenv("APP_SECRET_KEY", "k")
    clean_env.setenv(name, value)
    with pytest.raises(settings.ConfigError, match=match):
        settings.get_settings()


def test_invalid_dev_user(clean_env):
    clean_env.setenv("APP_ENV", "dev")
    clean_env.setenv("DEV_USER", "../Luis")
    with pytest.raises(settings.ConfigError, match="DEV_USER"):
        settings.get_settings()


def test_config_error_never_contains_secret(clean_env):
    clean_env.setenv("APP_SECRET_KEY", "k")
    clean_env.setenv("DEFAULT_ARL", "super-secret-arl-value")
    clean_env.setenv("MIN_FREE_GB", "x")
    with pytest.raises(settings.ConfigError) as e:
        settings.get_settings()
    assert "super-secret-arl-value" not in str(e.value)
