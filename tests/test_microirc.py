import json
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from microirc import (
    IRCSession,
    MicroIRCError,
    ServerConfig,
    advertised_commands,
    build_privmsg,
    discover_static_commands,
    load_server_configs,
    parse_irc_line,
)


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
