from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, List, Optional


class RedactingFormatter(logging.Formatter):
    """Mask Telegram tokens and configured secrets across messages and tracebacks."""

    _TOKEN_PATTERN = re.compile(r"(bot\d+:)[A-Za-z0-9_-]+")

    def __init__(
        self,
        fmt: str,
        datefmt: str,
        secrets: Iterable[str],
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._secrets: List[str] = [s for s in secrets if s and len(s) >= 4]

    def _redact(self, text: str) -> str:
        masked = self._TOKEN_PATTERN.sub(r"\1[REDACTED]", text)
        for secret in self._secrets:
            if secret in masked:
                masked = masked.replace(secret, "[REDACTED]")
        return masked

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return self._redact(formatted)


def setup_logger(
    level_name: str = "INFO",
    secrets: Iterable[str] = (),
    log_to_file: bool = False,
    log_file: Optional[Path] = None,
) -> logging.Logger:
    logger = logging.getLogger("qbot")
    level = getattr(logging, level_name.upper(), logging.INFO)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = RedactingFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        secrets=secrets,
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_to_file and log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=str(log_file),
            maxBytes=1_048_576,  # En fazla 1 MB
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger