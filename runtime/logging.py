from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(
    *,
    log_file: str | Path | None = None,
    stdout_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    logger = logging.getLogger("taleclaw")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(stdout_level)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
