#!/usr/bin/env python3
"""Small IRC smoke-test client for pyBurlyBot commands.

The client reads the normal BurlyBot JSON configuration, joins the configured
channels, then sends the module-aware cases in :data:`TEST_COMMANDS` one at a
time.  It deliberately does not load the bot runtime, databases, or modules.
"""

import argparse
import ast
import codecs
import json
import socket
import ssl
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

DEFAULT_CONFIG = "BurlyBot.json"
DEFAULT_PORT = 6667
REGISTRATION_ERRORS = {"432", "436", "437", "451", "462", "463", "464", "465", "466"}
JOIN_ERRORS = {"403", "405", "471", "473", "474", "475", "476", "477", "489"}


class MicroIRCError(Exception):
    """An expected configuration, connection, or IRC protocol failure."""


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    key: str | None = None


@dataclass(frozen=True)
class ServerConfig:
    label: str
    host: str
    port: int
    tls: bool
    verify: bool
    cert: str | None
    encoding: str
    bot_nick: str
    alt_bot_nicks: tuple[str, ...]
    nick_suffix: str
    command_prefix: str
    modules: tuple[str, ...]
    channels: tuple[ChannelConfig, ...]


@dataclass(frozen=True)
class IRCMessage:
    prefix: str | None
    command: str
    params: tuple[str, ...]
    raw: str

    @property
    def nick(self) -> str | None:
        if not self.prefix:
            return None
        return self.prefix.split("!", 1)[0]


@dataclass(frozen=True)
class TestCommand:
    """One deliberately selected public command invocation.

    ``multiline`` means the command can call ``bot.say`` more than once.  IRC
    has no reply correlation or end marker, so those cases are complete after
    the bot has been quiet for the configured multiline idle period.
    """

    module: str
    command: str
    arguments: str = ""
    multiline: bool = False

    def body(self, nickname: str) -> str:
        arguments = self.arguments.replace("{nick}", nickname)
        return " ".join(part for part in (self.command, arguments) if part)


# Keep this explicit.  Besides making argument handling testable, it gives us
# one obvious place to add awkward inputs and annotate commands whose callbacks
# may produce several PRIVMSG lines (help loops over mappings, state/time/seen
# can loop over records, and timers emits a heading followed by timer rows).
TEST_COMMANDS: tuple[TestCommand, ...] = (
    TestCommand("help", "commands"),
    TestCommand("help", "help", "calc", multiline=True),
    TestCommand("alert", "alert", "microirc-nobody at invalid-date harness test"),
    TestCommand("alias", "alias", "microirc-unknown"),
    TestCommand("alias", "group", "microirc-unknown"),
    TestCommand("alias", "subscripe", "microirc-unknown"),
    TestCommand("alias", "unsubscripe", "microirc-unknown"),
    TestCommand("butt", "butt", "argument handling should survive punctuation!"),
    TestCommand("butt", "butts", "~del -1"),
    TestCommand("calc", "calc", "1 + 1"),
    TestCommand("charinfo", "u", "2603"),
    TestCommand("codings", "hash", "sha256 microIRC"),
    TestCommand("codings", "md5", "microIRC"),
    TestCommand("codings", "rot13", "microIRC"),
    TestCommand("codings", "crc", "microIRC"),
    TestCommand("codings", "unquote", "microIRC%20test"),
    TestCommand("codings", "quote", "microIRC test"),
    TestCommand("codings", "encode", "utf-8 microIRC"),
    TestCommand("codings", "decode", "utf-8 microIRC"),
    TestCommand("gdq", "gdq", "~list"),
    TestCommand("gdqdonate", "gdqdonate"),
    TestCommand("google", "google", "pyBurlyBot"),
    TestCommand("google", "gis", "pyBurlyBot"),
    TestCommand("location", "location", "{nick}"),
    TestCommand("logindexsearch", "log", "1 microirc"),
    TestCommand("logindexsearch", "logstats", "microirc"),
    TestCommand("random", "rand", "10"),
    TestCommand("random", "choice", "alpha beta gamma"),
    TestCommand("random", "coinflip"),
    TestCommand("simplecommands", "simplecommands", "microirc-unknown"),
    TestCommand("state", "state", "network"),
    TestCommand("tell", "tell", "{nick} harness test"),
    TestCommand("tell", "remind", "microirc-nobody at invalid-date harness test"),
    TestCommand("time", "time", "{nick}", multiline=True),
    TestCommand("urbandictionary", "urbandictionary", "pyBurlyBot"),
    TestCommand("urlinfo", "head", "https://example.com/"),
    TestCommand("urlinfo", "title", "https://example.com/"),
    TestCommand("urlinfo", "lasturl"),
    TestCommand("users", "seen", "{nick}", multiline=True),
    TestCommand("weather", "weather", "{nick}"),
    TestCommand("weather", "forecast", "{nick}"),
    TestCommand("wikipedia", "wiki", "pyBurlyBot"),
    TestCommand("words", "dict", "test"),
    TestCommand("words", "spell", "mispeling"),
    TestCommand("words", "syn", "test"),
    TestCommand("youtube", "youtube", "pyBurlyBot"),
)


def _server_value(
    server: Mapping[str, Any], config: Mapping[str, Any], name: str, default: Any = None
) -> Any:
    return server.get(name) or config.get(name) or default


def _parse_port(value: Any) -> tuple[int, bool]:
    if value is None:
        port, tls = DEFAULT_PORT, False
    elif isinstance(value, int) and not isinstance(value, bool):
        port, tls = value, False
    elif isinstance(value, str):
        tls = value.startswith("+")
        port_text = value[1:] if tls else value
        try:
            port = int(port_text)
        except ValueError as exc:
            raise MicroIRCError(f"Invalid IRC port: {value!r}") from exc
    else:
        raise MicroIRCError("IRC port must be an integer or string")
    if not 1 <= port <= 65535:
        raise MicroIRCError(f"IRC port is outside the range 1-65535: {port}")
    return port, tls


def _parse_channels(value: Any) -> tuple[ChannelConfig, ...]:
    channels: list[ChannelConfig] = []
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MicroIRCError("Configured channels must be a list")
    for item in value:
        if isinstance(item, str):
            channels.append(ChannelConfig(item))
        elif isinstance(item, list) and item and isinstance(item[0], str):
            key = item[1] if len(item) > 1 and item[1] else None
            if key is not None and not isinstance(key, str):
                raise MicroIRCError(f"Channel key must be a string: {item!r}")
            channels.append(ChannelConfig(item[0], key))
        else:
            raise MicroIRCError(f"Invalid channel entry: {item!r}")
    return tuple(channels)


def load_server_configs(
    config_path: Path, server_label: str | None = None, channel_name: str | None = None
) -> tuple[ServerConfig, ...]:
    """Load connection targets without initializing the full bot runtime."""
    try:
        with config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except OSError as exc:
        raise MicroIRCError(f"Cannot read config {config_path}: {exc}") from exc
    except ValueError as exc:
        raise MicroIRCError(f"Config {config_path} is not valid JSON: {exc}") from exc

    if not isinstance(config, dict):
        raise MicroIRCError("Config root must be a JSON object")
    server_items: Any = config.get("servers") or ()
    if not isinstance(server_items, list):
        raise MicroIRCError("Config option 'servers' must be a list")

    targets: list[ServerConfig] = []
    known_labels: list[str] = []
    for item in server_items:
        if not isinstance(item, dict):
            raise MicroIRCError("Each configured server must be a JSON object")
        label = item.get("serverlabel")
        host = item.get("host")
        if (
            not isinstance(label, str)
            or not label
            or not isinstance(host, str)
            or not host
        ):
            raise MicroIRCError("Each configured server needs serverlabel and host")
        known_labels.append(label)
        if server_label is not None and label != server_label:
            continue

        channels = _parse_channels(item.get("channels"))
        if channel_name is not None:
            channels = tuple(
                channel
                for channel in channels
                if channel.name.lower() == channel_name.lower()
            )
            if not channels:
                continue
        if not channels:
            continue

        port, tls = _parse_port(item.get("port"))
        configured_modules: Any = (
            item.get("allowmodules") or config.get("modules") or ()
        )
        if not isinstance(configured_modules, (list, tuple)):
            raise MicroIRCError("Configured modules must be a list")
        if not all(isinstance(name, str) for name in configured_modules):
            raise MicroIRCError("Every configured module name must be a string")
        denied_value: Any = item.get("denymodules") or ()
        if not isinstance(denied_value, (list, tuple)) or not all(
            isinstance(name, str) for name in denied_value
        ):
            raise MicroIRCError("Every denied module name must be a string")
        denied_modules: set[str] = set(denied_value)
        modules: tuple[str, ...] = tuple(
            name for name in configured_modules if name not in denied_modules
        )
        alt_nicks: Any = _server_value(item, config, "altnicks", ())
        if isinstance(alt_nicks, str):
            alt_nicks = (alt_nicks,)
        if not isinstance(alt_nicks, (list, tuple)) or not all(
            isinstance(nick, str) for nick in alt_nicks
        ):
            raise MicroIRCError("Configured alternative nicknames must be strings")
        encoding = _server_value(item, config, "encoding", "utf-8")
        bot_nick = _server_value(item, config, "nick", "BurlyBot")
        nick_suffix = _server_value(item, config, "nicksuffix", "_")
        command_prefix = _server_value(item, config, "commandprefix", "!")
        cert: Any = _server_value(item, config, "cert")
        verify: Any = _server_value(item, config, "verify", True)
        if not all(
            isinstance(value, str)
            for value in (encoding, bot_nick, nick_suffix, command_prefix)
        ):
            raise MicroIRCError(
                "Encoding, nick, nick suffix, and command prefix must be strings"
            )
        if cert is not None and not isinstance(cert, str):
            raise MicroIRCError("Configured TLS certificate path must be a string")
        if not isinstance(verify, bool):
            raise MicroIRCError("Configured TLS verification option must be a boolean")
        if not command_prefix:
            raise MicroIRCError("Command prefix must not be empty")
        try:
            codecs.lookup(encoding)
        except LookupError as exc:
            raise MicroIRCError(f"Unknown configured encoding: {encoding}") from exc

        targets.append(
            ServerConfig(
                label=label,
                host=host,
                port=port,
                tls=tls,
                verify=verify,
                cert=cert,
                encoding=encoding,
                bot_nick=bot_nick,
                alt_bot_nicks=tuple(alt_nicks),
                nick_suffix=nick_suffix,
                command_prefix=command_prefix,
                modules=modules,
                channels=channels,
            )
        )

    if targets:
        return tuple(targets)
    if server_label is not None and server_label not in known_labels:
        raise MicroIRCError(
            "Server {!r} is not configured (available: {})".format(
                server_label, ", ".join(known_labels) or "none"
            )
        )
    if channel_name is not None:
        raise MicroIRCError(
            f"Channel {channel_name!r} is not configured on the selected server(s)"
        )
    raise MicroIRCError("No configured server has a channel to test")


def parse_irc_line(line: str) -> IRCMessage:
    """Parse the small RFC 1459 message subset needed by this client."""
    raw = line.rstrip("\r\n")
    rest = raw
    if rest.startswith("@"):
        _, separator, rest = rest.partition(" ")
        if not separator:
            raise MicroIRCError(f"Malformed IRC message: {raw!r}")
    prefix = None
    if rest.startswith(":"):
        prefix, separator, rest = rest[1:].partition(" ")
        if not separator:
            raise MicroIRCError(f"Malformed IRC message: {raw!r}")
    if " :" in rest:
        head, trailing = rest.split(" :", 1)
        parts = head.split()
        parts.append(trailing)
    else:
        parts = rest.split()
    if not parts:
        raise MicroIRCError("Empty IRC message")
    return IRCMessage(prefix, parts[0].upper(), tuple(parts[1:]), raw)


def advertised_commands(message: str) -> tuple[str, ...]:
    """Validate and de-duplicate the response produced by the help module."""
    commands: list[str] = []
    seen: set[str] = set()
    for command in message.split():
        if not command or any(ord(character) < 33 for character in command):
            return ()
        lowered = command.lower()
        if lowered not in seen:
            seen.add(lowered)
            commands.append(command)
    # A response to this project's commands command includes itself.  This check
    # prevents unrelated channel chatter from being treated as a command list.
    if "commands" not in seen:
        return ()
    return tuple(commands)


def _literal_keyword(call: ast.Call, name: str, default: Any = None) -> Any:
    for keyword in call.keywords:
        if keyword.arg == name:
            try:
                return ast.literal_eval(keyword.value)
            except ValueError, TypeError:
                return default
    return default


def discover_static_commands(
    modules: Iterable[str], module_dir: Path
) -> tuple[str, ...]:
    """Inspect module source for public primary mappings without importing it."""
    commands: set[str] = set()
    for module_name in modules:
        module_path = module_dir / (module_name + ".py")
        try:
            tree = ast.parse(
                module_path.read_text(encoding="utf-8"), filename=str(module_path)
            )
        except OSError, SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = getattr(node.func, "id", None) or getattr(
                node.func, "attr", None
            )
            if function_name != "Mapping":
                continue
            if _literal_keyword(node, "admin", False) or _literal_keyword(
                node, "hidden", False
            ):
                continue
            command = _literal_keyword(node, "command")
            if isinstance(command, str):
                commands.add(command)
            elif (
                isinstance(command, (list, tuple))
                and command
                and isinstance(command[0], str)
            ):
                commands.add(command[0])
    return tuple(sorted(commands, key=str.lower))


def select_test_commands(modules: Iterable[str]) -> tuple[TestCommand, ...]:
    """Return the hand-authored command cases belonging to enabled modules."""
    enabled = {module.lower() for module in modules}
    return tuple(case for case in TEST_COMMANDS if case.module.lower() in enabled)


def command_from_body(body: str, prefix: str) -> TestCommand:
    """Build a command-line override, inheriting known multiline metadata."""
    stripped = body.strip()
    stripped = stripped[len(prefix) :] if stripped.startswith(prefix) else stripped
    command, _, arguments = stripped.partition(" ")
    if not command:
        raise MicroIRCError("A command override must not be empty")
    known = next(
        (case for case in TEST_COMMANDS if case.command.lower() == command.lower()),
        None,
    )
    return TestCommand(
        module=known.module if known else "<command-line>",
        command=command,
        arguments=arguments,
        multiline=known.multiline if known else False,
    )


def build_privmsg(target: str, message: str) -> str:
    if not target or not message:
        raise MicroIRCError("PRIVMSG target and message must not be empty")
    if any(character in target + message for character in "\r\n\0"):
        raise MicroIRCError("IRC target and message must not contain CR, LF, or NUL")
    return f"PRIVMSG {target} :{message}"


class IRCSession:
    def __init__(
        self,
        settings: ServerConfig,
        nickname: str,
        timeout: float,
        verbose: bool = False,
    ) -> None:
        self.settings = settings
        self.nickname = nickname
        self.timeout = timeout
        self.verbose = verbose
        self.socket: socket.socket | None = None
        self.receive_buffer: bytes = b""

    def connect(self) -> None:
        try:
            connection: socket.socket = socket.create_connection(
                (self.settings.host, self.settings.port), timeout=self.timeout
            )
            if self.settings.tls:
                context: ssl.SSLContext = ssl.create_default_context()
                if not self.settings.verify:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                if self.settings.cert:
                    context.load_cert_chain(self.settings.cert)
                connection = context.wrap_socket(
                    connection, server_hostname=self.settings.host
                )
        except (OSError, ssl.SSLError) as exc:
            raise MicroIRCError(
                f"Cannot connect to {self.settings.label} ({self.settings.host}:{self.settings.port}): {exc}"
            ) from exc

        self.socket = connection
        self.send_line(f"NICK {self.nickname}")
        self.send_line("USER pyburlytest 0 * :pyBurlyBot command smoke test")
        self._wait_for_registration()

    def _wait_for_registration(self) -> None:
        deadline = monotonic() + self.timeout
        collisions: int = 0
        while True:
            try:
                message = self.receive(max(0.0, deadline - monotonic()))
            except TimeoutError as exc:
                raise MicroIRCError(
                    "Timed out while registering with the IRC server"
                ) from exc
            if message.command == "001":
                return
            if message.command == "433":
                collisions += 1
                if collisions > 8:
                    raise MicroIRCError("Could not find an available test nickname")
                self.nickname += "_"
                self.send_line(f"NICK {self.nickname}")
            elif message.command in REGISTRATION_ERRORS or message.command == "ERROR":
                raise MicroIRCError(f"IRC registration failed: {message.raw}")

    def join(self, channel: ChannelConfig) -> None:
        line = f"JOIN {channel.name}"
        if channel.key:
            line += " " + channel.key
        self.send_line(line)
        deadline = monotonic() + self.timeout
        while True:
            try:
                message = self.receive(max(0.0, deadline - monotonic()))
            except TimeoutError as exc:
                raise MicroIRCError(f"Timed out while joining {channel.name}") from exc
            if (
                message.command == "JOIN"
                and message.nick
                and message.nick.lower() == self.nickname.lower()
                and message.params
                and message.params[0].lower() == channel.name.lower()
            ):
                return
            if message.command in JOIN_ERRORS and any(
                parameter.lower() == channel.name.lower()
                for parameter in message.params
            ):
                raise MicroIRCError(f"Could not join {channel.name}: {message.raw}")

    def send_line(self, line: str) -> None:
        connection = self.socket
        if connection is None:
            raise MicroIRCError("IRC socket is not connected")
        if any(character in line for character in "\r\n\0"):
            raise MicroIRCError("IRC lines must not contain CR, LF, or NUL")
        encoded = line.encode(self.settings.encoding)
        if len(encoded) > 510:
            raise MicroIRCError("IRC line is longer than 510 encoded bytes")
        if self.verbose:
            print("    >> " + line)
        try:
            connection.sendall(encoded + b"\r\n")
        except OSError as exc:
            raise MicroIRCError(f"IRC send failed: {exc}") from exc

    def send_privmsg(self, target: str, message: str) -> None:
        self.send_line(build_privmsg(target, message))

    def receive(self, timeout: float) -> IRCMessage:
        connection = self.socket
        if connection is None:
            raise MicroIRCError("IRC socket is not connected")
        if timeout <= 0:
            raise TimeoutError()
        deadline = monotonic() + timeout
        while True:
            while b"\n" not in self.receive_buffer:
                connection.settimeout(max(0.001, deadline - monotonic()))
                try:
                    chunk: bytes = connection.recv(4096)
                except TimeoutError:
                    raise
                except OSError as exc:
                    raise MicroIRCError(f"IRC receive failed: {exc}") from exc
                if not chunk:
                    raise MicroIRCError("IRC server closed the connection")
                self.receive_buffer += chunk
                if len(self.receive_buffer) > 65536:
                    raise MicroIRCError(
                        "IRC server sent an excessively long unterminated line"
                    )
            line, self.receive_buffer = self.receive_buffer.split(b"\n", 1)
            message = parse_irc_line(line.decode(self.settings.encoding, "replace"))
            if self.verbose:
                print("    << " + message.raw)
            if message.command == "PING":
                token = message.params[-1] if message.params else ""
                self.send_line(f"PONG :{token}")
                continue
            if message.command == "ERROR":
                raise MicroIRCError(f"IRC server error: {message.raw}")
            return message

    def _is_bot_nick(self, nickname: str | None) -> bool:
        if nickname is None:
            return False
        candidate = nickname.lower()
        for configured in (self.settings.bot_nick, *self.settings.alt_bot_nicks):
            base = configured.lower()
            if candidate == base:
                return True
            suffix = self.settings.nick_suffix.lower()
            if suffix and candidate.startswith(base):
                remainder = candidate[len(base) :]
                if remainder and not remainder.replace(suffix, ""):
                    return True
        return False

    def wait_for_result(
        self,
        command: TestCommand,
        channel: ChannelConfig,
        timeout: float,
        multiline_idle: float,
    ) -> int:
        """Wait for this command's reply, or for its deadline to expire."""
        deadline = monotonic() + timeout
        idle_deadline: float | None = None
        replies = 0
        while True:
            wait_until = min(deadline, idle_deadline or deadline)
            try:
                message = self.receive(max(0.0, wait_until - monotonic()))
            except TimeoutError:
                return replies
            if (
                message.command == "KICK"
                and len(message.params) > 1
                and message.params[1].lower() == self.nickname.lower()
            ):
                raise MicroIRCError(f"Test client was kicked: {message.raw}")
            if message.command in {"PRIVMSG", "NOTICE"} and len(message.params) >= 2:
                target = message.params[0].lower()
                expected_targets = {channel.name.lower(), self.nickname.lower()}
                if self._is_bot_nick(message.nick) and target in expected_targets:
                    print(f"      <{message.nick}> {message.params[-1]}")
                    replies += 1
                    if not command.multiline:
                        return replies
                    idle_deadline = monotonic() + multiline_idle
            elif message.command.isdigit() and int(message.command) >= 400:
                print(f"      [server] {message.raw}")

    def close(self) -> None:
        if self.socket is None:
            return
        try:
            self.send_line("QUIT :Smoke test complete")
        except MicroIRCError:
            pass
        connection = self.socket
        if connection is not None:
            connection.close()
        self.socket = None
        self.receive_buffer = b""


def _command_message(prefix: str, command: str) -> str:
    return command if command.startswith(prefix) else prefix + command


def run_target(
    settings: ServerConfig,
    nickname: str,
    commands: Sequence[str],
    timeout: float,
    reply_timeout: float,
    multiline_idle: float,
    verbose: bool,
) -> int:
    tls_label = " TLS" if settings.tls else ""
    print(
        f"Connecting to {settings.label} at {settings.host}:{settings.port}{tls_label} as {nickname}"
    )
    session = IRCSession(settings, nickname, timeout, verbose)
    try:
        session.connect()
        print(f"  Registered as {session.nickname}")
        for channel in settings.channels:
            print(f"  Joining {channel.name}")
            session.join(channel)
            if commands:
                selected_commands = tuple(
                    command_from_body(body, settings.command_prefix)
                    for body in commands
                )
                print(
                    f"    Using {len(selected_commands)} command(s) supplied on the command line"
                )
            else:
                selected_commands = select_test_commands(settings.modules)
                if not selected_commands:
                    raise MicroIRCError(
                        "No static test commands belong to the configured modules"
                    )
                print(f"    Using {len(selected_commands)} static command case(s)")

            total_commands = len(selected_commands)
            print(f"    Exercising {total_commands} commands")
            for index, command in enumerate(selected_commands, 1):
                message = _command_message(
                    settings.command_prefix, command.body(session.nickname)
                )
                print(f"      [{index}/{total_commands}] {message}")
                session.send_privmsg(channel.name, message)
                replies = session.wait_for_result(
                    command, channel, reply_timeout, multiline_idle
                )
                if not replies:
                    print(f"      [timeout after {reply_timeout:g}s: no bot reply]")
    finally:
        session.close()
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send every public pyBurlyBot command as an IRC PRIVMSG smoke test."
    )
    parser.add_argument(
        "config", nargs="?", default=DEFAULT_CONFIG, help="BurlyBot JSON config"
    )
    parser.add_argument(
        "--server", help="test only this serverlabel (default: every configured server)"
    )
    parser.add_argument(
        "--channel", help="test only this channel (default: every configured channel)"
    )
    parser.add_argument(
        "--nick", help="test client nickname (default: configured bot nick plus 'Test')"
    )
    parser.add_argument(
        "--command",
        action="append",
        default=[],
        metavar="COMMAND",
        help="test this command/body instead of the static cases; may be repeated",
    )
    parser.add_argument(
        "--multiline-idle",
        "--delay",
        dest="multiline_idle",
        type=float,
        default=1.5,
        help="quiet seconds marking the end of a multiline reply",
    )
    parser.add_argument(
        "--reply-timeout",
        "--response-wait",
        dest="reply_timeout",
        type=float,
        default=20.0,
        help="maximum seconds to wait for each command's reply",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="connection, registration, and join timeout",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show targets and selected static command cases only",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show raw IRC traffic"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.multiline_idle <= 0 or args.reply_timeout <= 0 or args.timeout <= 0:
        parser.error("multiline-idle, reply-timeout, and timeout must be positive")

    try:
        config_path = Path(args.config)
        targets = load_server_configs(config_path, args.server, args.channel)
        if args.dry_run:
            for target in targets:
                channels = ", ".join(channel.name for channel in target.channels)
                nickname = args.nick or (target.bot_nick + "Test")
                commands = (
                    tuple(
                        command_from_body(body, target.command_prefix)
                        for body in args.command
                    )
                    if args.command
                    else select_test_commands(target.modules)
                )
                print(f"{target.label} {target.host}:{target.port} -> {channels}")
                for command in commands:
                    suffix = " [multiline]" if command.multiline else ""
                    print(f"  {command.body(nickname)}{suffix}")
            return 0

        for target in targets:
            nickname = args.nick or (target.bot_nick + "Test")
            run_target(
                target,
                nickname,
                args.command,
                args.timeout,
                args.reply_timeout,
                args.multiline_idle,
                args.verbose,
            )
        return 0
    except (MicroIRCError, UnicodeError) as exc:
        print(f"microirc: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nmicroirc: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
