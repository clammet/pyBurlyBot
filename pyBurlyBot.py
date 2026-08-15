from typing import IO
from types import FrameType
from typing import Any
# pyBurlyBot

from os import name

from os.path import exists
from os import environ, getcwd
from pathlib import Path
from sys import exit, stdout
from argparse import ArgumentParser
from threading import Event
import signal

# twisted imports
from twisted.python import log
from twisted.internet import reactor as _reactor
from twisted.internet.task import LoopingCall

# BurlyBot imports
from util.settings import Settings, ConfigException

reactor: Any = _reactor


def start_logging(output: IO[str]) -> Any:
    """Start Twisted logging without replacing multiprocessing-safe stdio."""
    return log.startLogging(output, setStdout=False)


def setup_sighup_handler() -> None:
    """
    Handle SIGHUP, received by screen children when screen receives SIGTERM
    """

    def sighup_handler(signum: int, frame: FrameType | None) -> None:
        reactor.callFromThread(reactor.stop)

    signal.signal(signal.SIGHUP, sighup_handler)


_shutting_down = Event()


def graceful_shutdown() -> None:
    """Disconnect from IRC cleanly (QUIT) before stopping the reactor."""
    if _shutting_down.is_set():
        return
    _shutting_down.set()
    if Settings.databasemanager is not None:
        Settings.shutdown()
    else:
        reactor.stop()


def setup_sigterm_handler() -> None:
    """
    Replace Twisted's default SIGTERM handler (bare reactor.stop, no IRC QUIT)
    with a graceful shutdown, so `docker stop`/systemd get a clean quit.
    Twisted installs its own handlers during reactor startup, so this must run
    via callWhenRunning to win.
    """

    def sigterm_handler(signum: int, frame: FrameType | None) -> None:
        reactor.callFromThread(graceful_shutdown)

    signal.signal(signal.SIGTERM, sigterm_handler)


def setup_heartbeat(path: str, interval: float = 30.0) -> None:
    """
    Periodically touch PATH from the reactor thread so container healthchecks
    can detect a stalled/hung process. Only active when PYBB_HEARTBEAT_FILE is
    set (i.e. when running in a container).
    """
    heartbeat = Path(path)

    def beat() -> None:
        try:
            heartbeat.touch()
        except OSError as e:
            print("Warning: heartbeat write failed: %s" % e)

    LoopingCall(beat).start(interval, now=True)


if __name__ == "__main__":
    # TODO: make botdir an argument maybe
    Settings.botdir = getcwd()

    # temporary logging
    templog = start_logging(stdout)
    print("Starting pyBurlyBot, press CTRL+C to quit.")

    parser = ArgumentParser(
        description="Internet bort pyBurlyBot",
        epilog="pyBurlyBot requires a config file to be specified to run.",
    )
    parser.add_argument(
        "-c",
        "--create-config",
        action="store_true",
        dest="createconfig",
        default=False,
        help="Creates example config. CONFIGFILE if specified else BurlyBot.json",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        dest="force",
        default=False,
        help="Force overwrite of existing config when creating config.",
    )
    # CONSIDER: this could easily support multiple config files I guess
    #   but changing Settings to support this would be kind of intense I think.
    parser.add_argument(
        "config", nargs="?", metavar="CONFIGFILE", default="BurlyBot.json"
    )

    args = parser.parse_args()

    # create-config
    if args.createconfig:
        print("Creating configuration...")
        if exists(args.config) and not args.force:
            print(
                "Error: NEWCONFIGFILE (%s) exists. Use --force (-f) to force overwrite. Bailing."
                % args.config
            )
            exit(1)
        Settings.configfile = args.config
        Settings.saveOptions()
        print("Done.")
        exit(0)

    if args.config and exists(args.config):
        Settings.configfile = args.config
    else:
        print("Error: Settings file (%s) not found." % args.config)
        exit(2)
    try:
        Settings.load()
    except ConfigException as e:
        print("Error:", e)
        exit(2)

    Settings.initialize(logger=templog)

    # Handle SIGHUP, signal received by screen children when screen receives SIGTERM
    # only when not windows...
    if name != "nt":
        setup_sighup_handler()
        reactor.callWhenRunning(setup_sigterm_handler)

    heartbeat_file = environ.get("PYBB_HEARTBEAT_FILE")
    if heartbeat_file:
        reactor.callWhenRunning(setup_heartbeat, heartbeat_file)
    # start reactor (which in a sense starts bot proper)
    reactor.run()
    Settings.hardshutdown()
