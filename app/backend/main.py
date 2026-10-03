"""Server entrypoint: python -m app.backend.main

Configures logging first, so the secrets filter installed at startup attaches to
the root handlers, and runs uvicorn with log_config=None so its loggers propagate
to that same (filtered) root handler.
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from app.backend.api import create_app
from app.backend.settings import ConfigError, get_settings


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = get_settings()  # also loads .env, so HOST/PORT may come from it
        host = os.environ.get("HOST", "").strip() or "127.0.0.1"
        try:
            port = int(os.environ.get("PORT", "").strip() or 8000)
        except ValueError:
            raise ConfigError("PORT must be a number") from None
    except ConfigError as e:
        print(f"Configuração inválida: {e}", file=sys.stderr)
        return 2
    if settings.app_env == "dev":
        logging.getLogger(__name__).info("APP_ENV=dev (DEV_USER=%s)", settings.dev_user or "-")
    uvicorn.run(create_app(settings), host=host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
