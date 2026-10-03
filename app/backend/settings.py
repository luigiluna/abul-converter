"""Configuration from the environment, with an optional .env file at the project root.

Real environment variables win over .env, so Docker/systemd can override it.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> None:
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def default_arl() -> str | None:
    load_dotenv()
    return os.environ.get("DEFAULT_ARL") or os.environ.get("DEEZLOAD_ARL") or None


class ConfigError(ValueError):
    """Bad or missing configuration. Messages name the variable, never its value."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secret_key: str
    default_arl: str | None
    max_parallel_downloads: int = 4
    max_user_concurrency: int = 3
    job_ttl_hours: float = 6
    min_free_gb: float = 2
    app_env: str = "prod"
    dev_user: str | None = None


# Only for APP_ENV=dev, so a local run works without generating a key.
DEV_SECRET_KEY = "spot2deez-dev-only-not-secret"


def _number(name: str, default, cast, minimum):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


@functools.cache
def get_settings() -> Settings:
    """Read the configuration once. Raises ConfigError with a clear message."""
    from app.backend.users import valid_username

    load_dotenv()
    app_env = os.environ.get("APP_ENV", "prod").strip().lower() or "prod"
    if app_env not in ("prod", "dev"):
        raise ConfigError("APP_ENV must be 'prod' or 'dev'")

    secret_key = os.environ.get("APP_SECRET_KEY", "").strip()
    if not secret_key:
        if app_env == "prod":
            raise ConfigError("APP_SECRET_KEY is required when APP_ENV=prod "
                              "(generate one with: python -c \"import secrets; print(secrets.token_hex(32))\")")
        secret_key = DEV_SECRET_KEY

    dev_user = os.environ.get("DEV_USER", "").strip() or None
    if app_env != "dev":
        dev_user = None
    elif dev_user and not valid_username(dev_user):
        raise ConfigError("DEV_USER must match [a-z0-9_-]{1,32}")

    data_dir = os.environ.get("DATA_DIR", "").strip()
    return Settings(
        data_dir=Path(data_dir) if data_dir else ROOT / "data",
        secret_key=secret_key,
        default_arl=default_arl(),
        max_parallel_downloads=_number("MAX_PARALLEL_DOWNLOADS", 4, int, 1),
        max_user_concurrency=_number("MAX_USER_CONCURRENCY", 3, int, 1),
        job_ttl_hours=_number("JOB_TTL_HOURS", 6.0, float, 0),
        min_free_gb=_number("MIN_FREE_GB", 2.0, float, 0),
        app_env=app_env,
        dev_user=dev_user,
    )
