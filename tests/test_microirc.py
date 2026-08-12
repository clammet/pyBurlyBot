import json
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import IO, cast
from unittest import TestCase

from microirc import (
    ChannelConfig,
    IRCMessage,
    IRCSession,
    MicroIRCError,
    ServerConfig,
    TEST_COMMANDS,
    TestCommand,
    advertised_commands,
    build_privmsg,
    command_from_body,
    discover_static_commands,
    load_server_configs,
    parse_irc_line,
    select_test_commands,
)
from microirc_server import IRCRelayServer


class MicroIRCTest(TestCase):
    def test_loads_bot_connection_settings_and_filters_targets(self) -> None:
        config = {
            "nick": "BürlyBot",
            "commandprefix": "??",
            "encoding": "utf-8",
            "modules": ["help", "calc", "blocked"],
            "servers": [
                {
                    "serverlabel": "example",
                    "host": "irc.example.net",
                    "port": "+6697",
                    "channels": ["#one", ["#two", "secret"]],
                    "denymodules": ["blocked"],
                }
            ],
        }
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "BurlyBot.json")
            config_path.write_text(json.dumps(config), encoding="utf-8")

            (target,) = load_server_configs(config_path, "example", "#two")

        self.assertEqual(target.host, "irc.example.net")
        self.assertEqual(target.port, 6697)
        self.assertTrue(target.tls)
        self.assertEqual(target.bot_nick, "BürlyBot")
        self.assertEqual(target.command_prefix, "??")
        self.assertEqual(target.modules, ("help", "calc"))
        self.assertEqual(target.channels[0].name, "#two")
        self.assertEqual(target.channels[0].key, "secret")

    def test_parses_tagged_privmsg_and_command_list(self) -> None:
        message = parse_irc_line(
            "@time=123 :BurlyBot!bot@example PRIVMSG #test :calc commands help help"
        )

        self.assertEqual(message.command, "PRIVMSG")
        self.assertEqual(message.nick, "BurlyBot")
        self.assertEqual(message.params, ("#test", "calc commands help help"))
        self.assertEqual(
            advertised_commands(message.params[-1]), ("calc", "commands", "help")
        )

    def test_build_privmsg_rejects_irc_line_injection(self) -> None:
        self.assertEqual(
            build_privmsg("#test", "!calc 1+1"), "PRIVMSG #test :!calc 1+1"
        )
        with self.assertRaises(MicroIRCError):
            build_privmsg("#test", "!help\r\nQUIT")

    def test_session_answers_ping_and_preserves_buffered_privmsg(self) -> None:
        settings = ServerConfig(
            label="test",
            host="localhost",
            port=6667,
            tls=False,
            verify=False,
            cert=None,
            encoding="utf-8",
            bot_nick="BurlyBot",
            alt_bot_nicks=(),
            nick_suffix="_",
            command_prefix="!",
            modules=(),
            channels=(),
        )
        client_socket, server_socket = socket.socketpair()
        session = IRCSession(settings, "BurlyBotTest", timeout=1)
        session.socket = client_socket
        try:
            server_socket.sendall(
                b"PING :server-token\r\n"
                b":BurlyBot!bot@example PRIVMSG #test :commands help\r\n"
            )

            message = session.receive(1)

            self.assertEqual(message.command, "PRIVMSG")
            self.assertEqual(message.params[-1], "commands help")
            self.assertEqual(server_socket.recv(1024), b"PONG :server-token\r\n")
        finally:
            session.close()
            server_socket.close()

    def test_static_discovery_uses_primary_non_admin_non_hidden_commands(self) -> None:
        module_source = (
            "mappings = (\n"
            "    Mapping(command=('primary', 'alias')),\n"
            "    Mapping(command='admin', admin=True),\n"
            "    Mapping(command='internal', hidden=True),\n"
            ")\n"
        )
        with TemporaryDirectory() as temp_dir:
            module_dir = Path(temp_dir)
            module_dir.joinpath("demo.py").write_text(module_source, encoding="utf-8")

            commands = discover_static_commands(("demo",), module_dir)

        self.assertEqual(commands, ("primary",))

    def test_static_cases_cover_public_mappings_and_supply_arguments(self) -> None:
        module_dir = Path(__file__).parent.parent / "pyburlybot_modules"
        modules = tuple(path.stem for path in module_dir.glob("*.py"))

        discovered = set(discover_static_commands(modules, module_dir))
        configured = {command.command for command in TEST_COMMANDS}
        selected = select_test_commands(("calc", "timerexample"))

        self.assertEqual(configured, discovered)
        self.assertEqual(
            [command.body("TestNick") for command in selected],
            ["calc 1 + 1", "timers show"],
        )
        self.assertFalse(selected[0].multiline)
        self.assertTrue(selected[1].multiline)
        self.assertEqual(
            command_from_body("!timers show", "!"),
            TestCommand("timerexample", "timers", "show", multiline=True),
        )

    def test_result_wait_consumes_one_or_all_multiline_replies(self) -> None:
        settings = ServerConfig(
            label="test",
            host="localhost",
            port=6667,
            tls=False,
            verify=False,
            cert=None,
            encoding="utf-8",
            bot_nick="BurlyBot",
            alt_bot_nicks=(),
            nick_suffix="_",
            command_prefix="!",
            modules=(),
            channels=(),
        )
        client_socket, server_socket = socket.socketpair()
        session = IRCSession(settings, "BurlyBotTest", timeout=1)
        session.socket = client_socket
        try:
            server_socket.sendall(
                b":BurlyBot!bot@example PRIVMSG #test :first\r\n"
                b":BurlyBot!bot@example PRIVMSG #test :second\r\n"
            )

            replies = session.wait_for_result(
                TestCommand("demo", "single"),
                channel=self._channel("#test"),
                timeout=0.2,
                multiline_idle=0.01,
            )

            self.assertEqual(replies, 1)
            self.assertEqual(session.receive(0.2).params[-1], "second")

            server_socket.sendall(
                b":BurlyBot!bot@example PRIVMSG #test :line one\r\n"
                b":BurlyBot!bot@example PRIVMSG #test :line two\r\n"
            )
            replies = session.wait_for_result(
                TestCommand("demo", "multi", multiline=True),
                channel=self._channel("#test"),
                timeout=0.2,
                multiline_idle=0.01,
            )

            self.assertEqual(replies, 2)
        finally:
            session.close()
            server_socket.close()

    def test_micro_server_relays_channel_messages_between_two_clients(self) -> None:
        server = IRCRelayServer(("127.0.0.1", 0))
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        bot_socket: socket.socket | None = None
        test_socket: socket.socket | None = None
        try:
            raw_address = server.server_address
            assert isinstance(raw_address[0], str) and len(raw_address) == 2
            address = (raw_address[0], raw_address[1])
            bot_socket, bot_file = self._register_client(address, "BurlyBot")
            test_socket, test_file = self._register_client(address, "BurlyBotTest")
            self._join(bot_file, "BurlyBot", "#test")
            self._join(test_file, "BurlyBotTest", "#test")

            test_file.write(b"PRIVMSG #test :!calc 1 + 1\r\n")
            request = self._read_until(bot_file, "PRIVMSG")

            self.assertEqual(request.nick, "BurlyBotTest")
            self.assertEqual(request.params, ("#test", "!calc 1 + 1"))

            bot_file.write(b"PRIVMSG #test :2\r\n")
            response = self._read_until(test_file, "PRIVMSG")

            self.assertEqual(response.nick, "BurlyBot")
            self.assertEqual(response.params, ("#test", "2"))
        finally:
            for connection in (bot_socket, test_socket):
                if connection is not None:
                    connection.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1)

    @staticmethod
    def _channel(name: str) -> ChannelConfig:
        return ChannelConfig(name)

    def _register_client(
        self, address: tuple[str, int], nickname: str
    ) -> tuple[socket.socket, IO[bytes]]:
        connection = socket.create_connection(address, timeout=1)
        connection.settimeout(1)
        stream = cast(IO[bytes], connection.makefile("rwb", buffering=0))
        stream.write(f"NICK {nickname}\r\nUSER test 0 * :Test User\r\n".encode())
        self._read_until(stream, "376")
        return connection, stream

    def _join(self, stream: IO[bytes], nickname: str, channel: str) -> None:
        stream.write(f"JOIN {channel}\r\n".encode())
        message = self._read_until(stream, "JOIN")
        self.assertEqual(message.nick, nickname)
        self._read_until(stream, "366")

    @staticmethod
    def _read_until(stream: IO[bytes], command: str) -> IRCMessage:
        while True:
            encoded = stream.readline()
            if not encoded:
                raise AssertionError("IRC server closed the test connection")
            message = parse_irc_line(encoded.decode())
            if message.command == command:
                return message
