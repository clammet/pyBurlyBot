#!/usr/bin/env python3
"""Run pyBurlyBot and the microIRC client against the local relay server."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from typing import Any, TextIO, cast

from microirc_server import IRCRelayServer

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = PROJECT_ROOT / "microirc_harness.conf"
DEFAULT_SECRETS_CONFIG = PROJECT_ROOT / "BurlyBot.json"
API_CREDENTIAL_OPTIONS = frozenset({"API_KEY", "CSE_ID"})
EXCLUDED_HARNESS_MODULES: Mapping[str, str] = {
    "remind_common": "shared helper library for tell/alert, not a bot module",
    "updaterelaunch": "mutates and restarts the source checkout",
}


class HarnessError(Exception):
    """An expected configuration, process, or readiness failure."""


@dataclass(frozen=True)
class HarnessTarget:
    server_index: int
    server_label: str
    host: str
    port: int
    channel: str
    bot_nickname: str
    encoding: str


class ProcessCapture:
    """Continuously drain child output while retaining a diagnostic tail."""

    def __init__(self, process: subprocess.Popen[str], *, echo: bool) -> None:
        stream = process.stdout
        if stream is None:
            raise HarnessError("Child process output was not captured")
        self.stream = cast(TextIO, stream)
        self.echo = echo
        self.lines: deque[str] = deque(maxlen=100)
        self.thread = Thread(
            target=self._drain, name="microirc-bot-output", daemon=True
        )
        self.thread.start()

    def _drain(self) -> None:
        for line in self.stream:
            clean = line.rstrip("\r\n")
            self.lines.append(clean)
            if self.echo:
                print(f"[bot] {clean}", flush=True)

    def tail(self) -> str:
        if not self.lines:
            return ""
        return "\n".join(f"  {line}" for line in self.lines)

    def join(self, timeout: float = 1.0) -> None:
        self.thread.join(timeout)


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except OSError as exc:
        raise HarnessError(f"Cannot read config {path}: {exc}") from exc
    except ValueError as exc:
        raise HarnessError(f"Config {path} is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise HarnessError(f"Config {path} must contain a JSON object")
    return config


def overlay_api_credentials(
    config: dict[str, Any], secrets: Mapping[str, Any]
) -> tuple[str, ...]:
    """Copy only opted-in API credentials from a normal bot configuration."""
    destinations = config.get("moduleopts")
    if not isinstance(destinations, dict):
        return ()

    sources: list[Mapping[str, Any]] = []
    global_options = secrets.get("moduleopts")
    if isinstance(global_options, Mapping):
        sources.append(global_options)
    servers = secrets.get("servers")
    if isinstance(servers, list):
        for server in servers:
            if not isinstance(server, Mapping):
                continue
            server_options = server.get("moduleopts")
            if isinstance(server_options, Mapping):
                sources.append(server_options)

    imported: list[str] = []
    for module_name, destination_options in destinations.items():
        if not isinstance(module_name, str) or not isinstance(
            destination_options, dict
        ):
            continue
        for option_name in destination_options:
            if option_name not in API_CREDENTIAL_OPTIONS:
                continue
            for source in sources:
                source_options = source.get(module_name)
                if not isinstance(source_options, Mapping):
                    continue
                value = source_options.get(option_name)
                if value is None or value == "":
                    continue
                destination_options[option_name] = deepcopy(value)
                imported.append(f"{module_name}.{option_name}")
                break
    return tuple(sorted(imported))


def harness_target(
    config: Mapping[str, Any],
    *,
    host_override: str | None = None,
    port_override: int | None = None,
) -> HarnessTarget:
    servers = config.get("servers")
    if not isinstance(servers, list) or not servers:
        raise HarnessError("Harness config must contain at least one IRC server")
    server_index = next(
        (
            index
            for index, server in enumerate(servers)
            if isinstance(server, Mapping)
            and server.get("serverlabel") == "microirc-harness"
        ),
        0,
    )
    server = servers[server_index]
    if not isinstance(server, Mapping):
        raise HarnessError("Harness IRC server entry must be an object")

    label = server.get("serverlabel")
    host = host_override or server.get("host")
    raw_port = port_override if port_override is not None else server.get("port")
    nickname = config.get("nick")
    encoding = config.get("encoding", "utf-8")
    channels = server.get("channels")
    if not all(
        isinstance(value, str) and value for value in (label, host, nickname, encoding)
    ):
        raise HarnessError(
            "Harness config needs non-empty serverlabel, host, nick, and encoding values"
        )
    if isinstance(raw_port, bool) or not isinstance(raw_port, (int, str)):
        raise HarnessError(
            "Harness IRC port must be an integer or plaintext port string"
        )
    if isinstance(raw_port, str) and raw_port.startswith("+"):
        raise HarnessError("The microIRC server does not implement TLS")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise HarnessError(f"Invalid harness IRC port: {raw_port!r}") from exc
    if not 0 <= port <= 65535:
        raise HarnessError("Harness IRC port must be in the range 0-65535")
    if not isinstance(channels, list) or not channels:
        raise HarnessError("Harness IRC server needs at least one channel")
    first_channel = channels[0]
    channel = (
        first_channel[0]
        if isinstance(first_channel, list) and first_channel
        else first_channel
    )
    if not isinstance(channel, str) or not channel:
        raise HarnessError("Harness channel must be a non-empty string")

    return HarnessTarget(
        server_index=server_index,
        server_label=cast(str, label),
        host=cast(str, host),
        port=port,
        channel=channel,
        bot_nickname=cast(str, nickname),
        encoding=cast(str, encoding),
    )


def write_runtime_config(
    config: Mapping[str, Any], runtime_dir: Path, target: HarnessTarget
) -> Path:
    """Write the isolated, possibly secret-bearing config with mode 0600."""
    runtime_config = deepcopy(dict(config))
    runtime_config["datadir"] = str(runtime_dir / "data")
    runtime_config["datafile"] = "microirc-harness.db"
    runtime_config.pop("logfile", None)

    servers = runtime_config.get("servers")
    if not isinstance(servers, list) or not isinstance(
        servers[target.server_index], dict
    ):
        raise HarnessError(
            "Harness IRC server entry changed while preparing runtime config"
        )
    server = servers[target.server_index]
    server["host"] = target.host
    server["port"] = target.port
    server["channels"] = [target.channel]
    server.pop("cert", None)
    server.pop("verify", None)

    module_options = runtime_config.setdefault("moduleopts", {})
    if not isinstance(module_options, dict):
        raise HarnessError("Harness moduleopts must be an object")
    log_options = module_options.setdefault("logindexsearch", {})
    paste_options = module_options.setdefault("selfpaste", {})
    if not isinstance(log_options, dict) or not isinstance(paste_options, dict):
        raise HarnessError("Harness log and paste module options must be objects")
    log_options["indexdir"] = str(runtime_dir / "logindex")
    paste_options["wwwroot"] = str(runtime_dir / "pastes")

    config_path = runtime_dir / "microirc-harness.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(config_path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
        json.dump(runtime_config, config_file, indent=4)
        config_file.write("\n")
    return config_path


def wait_for_bot(
    server: IRCRelayServer,
    process: subprocess.Popen[str],
    capture: ProcessCapture,
    target: HarnessTarget,
    timeout: float,
) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            tail = capture.tail()
            detail = f"\n{tail}" if tail else ""
            raise HarnessError(
                f"Bot exited during startup with status {return_code}{detail}"
            )
        if server.client_joined(target.bot_nickname, target.channel):
            return
        sleep(0.05)
    tail = capture.tail()
    detail = f"\n{tail}" if tail else ""
    raise HarnessError(
        f"Bot did not join {target.channel} within {timeout:g} seconds{detail}"
    )


def stop_process(process: subprocess.Popen[str], timeout: float) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def terminate_process(process: subprocess.Popen[str], timeout: float = 2.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run_harness(args: argparse.Namespace) -> int:
    template_path = Path(args.config_template).resolve()
    config = load_config(template_path)
    imported_credentials: tuple[str, ...] = ()
    if not args.no_secrets:
        secrets_path = Path(args.secrets_config).resolve()
        if secrets_path.exists():
            imported_credentials = overlay_api_credentials(
                config, load_config(secrets_path)
            )
        else:
            print(
                f"No secrets config at {secrets_path}; API-backed commands may fail",
                flush=True,
            )

    requested_target = harness_target(
        config, host_override=args.host, port_override=args.port
    )
    server = IRCRelayServer(
        (requested_target.host, requested_target.port),
        encoding=requested_target.encoding,
        verbose=args.verbose,
    )
    raw_address = server.server_address
    if len(raw_address) != 2 or not isinstance(raw_address[1], int):
        server.server_close()
        raise HarnessError("The microIRC server did not create an IPv4 endpoint")
    target = replace(requested_target, port=raw_address[1])
    server_thread = Thread(
        target=server.serve_forever, name="microirc-harness-server", daemon=True
    )

    with TemporaryDirectory(prefix="pyburlybot-microirc-") as temp_dir:
        runtime_dir = Path(temp_dir)
        try:
            runtime_config_path = write_runtime_config(config, runtime_dir, target)
        except (HarnessError, OSError, TypeError, ValueError):
            server.server_close()
            raise
        if imported_credentials:
            print(
                "Imported API options: " + ", ".join(imported_credentials),
                flush=True,
            )
        print(
            f"microIRC harness: {target.host}:{target.port} {target.channel} "
            f"as {target.bot_nickname}",
            flush=True,
        )

        try:
            server_thread.start()
        except RuntimeError as exc:
            server.server_close()
            raise HarnessError(f"Could not start the microIRC server: {exc}") from exc
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYBURLYBOT_HARNESS_CONFIG"] = str(runtime_config_path)
        bot_process: subprocess.Popen[str] | None = None
        client_process: subprocess.Popen[str] | None = None
        capture: ProcessCapture | None = None
        try:
            bot_process = subprocess.Popen(
                [
                    args.python,
                    str(PROJECT_ROOT / "pyBurlyBot.py"),
                    str(runtime_config_path),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            capture = ProcessCapture(bot_process, echo=args.verbose)
            wait_for_bot(
                server, bot_process, capture, target, timeout=args.startup_timeout
            )
            print("Bot joined; starting microIRC command client", flush=True)

            client_command = [
                args.python,
                str(PROJECT_ROOT / "microirc.py"),
                str(runtime_config_path),
                "--server",
                target.server_label,
                "--channel",
                target.channel,
                "--reply-timeout",
                str(args.reply_timeout),
                "--multiline-idle",
                str(args.multiline_idle),
            ]
            for command in args.command:
                client_command.extend(("--command", command))
            if args.verbose:
                client_command.append("--verbose")
            started_client = subprocess.Popen(
                client_command,
                cwd=PROJECT_ROOT,
                env=environment,
                text=True,
            )
            client_process = started_client
            while started_client.poll() is None:
                if bot_process.poll() is not None:
                    raise HarnessError(
                        "Bot exited while the command client was running"
                        + (f"\n{capture.tail()}" if capture.tail() else "")
                    )
                sleep(0.05)
            return_code = started_client.returncode
            if return_code:
                print(f"microIRC command client exited with status {return_code}")
            return return_code
        finally:
            if client_process is not None:
                terminate_process(client_process)
            if bot_process is not None:
                stop_process(bot_process, args.shutdown_timeout)
            if capture is not None:
                capture.join()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the bot and microIRC smoke client against the local relay."
    )
    parser.add_argument(
        "--config-template",
        default=str(DEFAULT_TEMPLATE),
        help="secret-free harness config template",
    )
    parser.add_argument(
        "--secrets-config",
        default=str(DEFAULT_SECRETS_CONFIG),
        help="normal BurlyBot config from which API options may be copied",
    )
    parser.add_argument(
        "--no-secrets",
        action="store_true",
        help="do not read API options from the normal bot config",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter used for the bot and command client",
    )
    parser.add_argument("--host", help="override the microIRC listen host")
    parser.add_argument(
        "--port",
        type=int,
        help="override the microIRC port; use 0 to select a free port",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=20.0,
        help="seconds to wait for the bot to register and join",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=8.0,
        help="seconds to allow graceful bot shutdown",
    )
    parser.add_argument(
        "--reply-timeout",
        type=float,
        default=20.0,
        help="maximum seconds for each command reply",
    )
    parser.add_argument(
        "--multiline-idle",
        type=float,
        default=1.5,
        help="quiet seconds marking the end of multiline replies",
    )
    parser.add_argument(
        "--command",
        action="append",
        default=[],
        metavar="COMMAND",
        help="run only this command body; may be repeated",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show bot and raw IRC traffic"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if any(
        value <= 0
        for value in (
            args.startup_timeout,
            args.shutdown_timeout,
            args.reply_timeout,
            args.multiline_idle,
        )
    ):
        parser.error("all timeout and idle values must be positive")
    try:
        return run_harness(args)
    except HarnessError as exc:
        print(f"microirc_harness: error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"microirc_harness: process/server error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nmicroirc_harness: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
