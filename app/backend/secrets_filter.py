"""Mask known secrets (ARLs) in every log record, as a last line of defence.

Logger filters only see records logged on that exact logger, not ones that
propagate from children, so install() also attaches the filter to the root
handlers that exist at that moment. Call it after logging is configured.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable

MASK = "***"
MIN_SECRET_LEN = 8  # never "mask" short strings such as "" or "1"


class SecretsFilter(logging.Filter):
    def __init__(self, get_secrets: Callable[[], Iterable[str]]):
        super().__init__()
        self.get_secrets = get_secrets

    def mask(self, text: str) -> str:
        for secret in self.get_secrets():
            if secret and len(secret) >= MIN_SECRET_LEN:
                text = text.replace(secret, MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # broken format args: leave the record alone
            return True
        record.msg, record.args = self.mask(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.mask(record.exc_text)
        return True


def install(get_secrets: Callable[[], Iterable[str]]) -> SecretsFilter:
    f = SecretsFilter(get_secrets)
    root = logging.getLogger()
    root.addFilter(f)
    for handler in root.handlers:
        handler.addFilter(f)
    return f


def uninstall(f: SecretsFilter) -> None:
    root = logging.getLogger()
    root.removeFilter(f)
    for handler in root.handlers:
        handler.removeFilter(f)
