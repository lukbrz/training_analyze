"""Logging for the web app, to find what's slow: page loads, UI actions, background jobs,
and stalls of the event loop (a handler doing slow work on it freezes every open page).

Lines go to stderr (the server's journal) and to data/logs/web.log (rotated, 5 x 2 MB).
TA_LOG_LEVEL=DEBUG also logs fast actions and every progress step of a job; the default,
INFO, logs page loads, actions slower than SLOW_ACTION_S, jobs and stalls.
"""

import asyncio
import logging
import os
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

import storage

LOG_DIR = storage.DATA_DIR / "logs"
SLOW_ACTION_S = 0.1  # UI actions faster than this are logged at DEBUG only
STALL_CHECK_S = 0.25
STALL_S = 0.3  # the event loop answering this much later than due counts as a stall

log = logging.getLogger("ta")


def setup(name: str) -> None:
    """Send the "ta" loggers to stderr and data/logs/<name>.log."""
    level = getattr(logging, os.environ.get("TA_LOG_LEVEL", "INFO").upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")]
    for handler in handlers:
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False


def duration(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.2f} s"


@contextmanager
def timed(what: str, logger: logging.Logger = log, slow_s: float = 0.0):
    """Log how long the block took (at DEBUG when under `slow_s`), and failures."""
    start = time.perf_counter()
    try:
        yield
    except Exception as e:
        logger.warning("%s failed after %s: %s", what, duration(time.perf_counter() - start), e)
        raise
    elapsed = time.perf_counter() - start
    logger.log(logging.INFO if elapsed >= slow_s else logging.DEBUG, "%s: %s", what, duration(elapsed))


async def watch_event_loop() -> None:
    """Log whenever the event loop was blocked: while it is, no page gets any update."""
    stalls = logging.getLogger("ta.stall")
    loop = asyncio.get_running_loop()
    while True:
        due = loop.time() + STALL_CHECK_S
        await asyncio.sleep(STALL_CHECK_S)
        late = loop.time() - due
        if late >= STALL_S:
            stalls.warning("event loop blocked for %s (pages froze meanwhile; see the action logged just before)", duration(late))
