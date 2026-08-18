"""Central logging setup (#10).

Stdlib logging is the one true log: every core file logs through
logging.getLogger(__name__), and Twisted's legacy log is bridged in via
PythonLoggingObserver. Nothing here replaces stdio, so multiprocessing
keeps working.
"""

from logging import (
    DEBUG,
    INFO,
    FileHandler,
    Formatter,
    Handler,
    StreamHandler,
    getLogger,
)
from typing import IO

from twisted.python import log as twisted_log

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"


class ConsoleLog:
    """Handle for the console handler; Settings.initialize stops it when
    console=False."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler

    def stop(self) -> None:
        getLogger().removeHandler(self.handler)


def start_logging(output: IO[str]) -> ConsoleLog:
    handler = StreamHandler(output)
    handler.setFormatter(Formatter(LOG_FORMAT))
    root = getLogger()
    root.addHandler(handler)
    root.setLevel(INFO)
    twisted_log.PythonLoggingObserver(loggerName="twisted").start()
    return ConsoleLog(handler)


def add_logfile(path: str) -> None:
    handler = FileHandler(path, encoding="utf-8")
    handler.setFormatter(Formatter(LOG_FORMAT))
    getLogger().addHandler(handler)


def set_debug(debug: int) -> None:
    getLogger().setLevel(DEBUG if debug else INFO)
