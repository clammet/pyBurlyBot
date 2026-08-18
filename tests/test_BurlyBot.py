from base64 import b64encode
from collections import deque
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast
from unittest import TestCase
from unittest.mock import Mock

from twisted.internet.testing import StringTransport

from util.client import BurlyBot
from util.helpers import coerceToUnicode, splitEncodedUnicode


class BurlyBotProtocolTest(TestCase):
    def test_logging_allows_multiprocessing_to_start(self) -> None:
        script = (
            "from multiprocessing import Process\n"
            "from sys import stdout\n"
            "from time import sleep\n"
            "from pyBurlyBot import start_logging\n"
            "start_logging(stdout)\n"
            "process = Process(target=sleep, args=(0,))\n"
            "process.start()\n"
            "process.join()\n"
            "raise SystemExit(process.exitcode)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def make_protocol(self, encoding: Any = "utf-8") -> BurlyBot:
        protocol = BurlyBot()
        protocol.settings = SimpleNamespace(
            encoding=encoding,
            sasl_username=None,
            sasl_password=None,
            sasl_authzid=None,
            serverlabel="test",
        )
        protocol.debug = 0
        protocol.transport = StringTransport()  # type: ignore[assignment]
        protocol._dqueue = deque()
        protocol._lastmsg = 0
        protocol._lines = 0
        protocol._lastCL = None
        protocol._accounts = {}
        protocol._message_tags = {}
        return protocol

    def test_send_line_encodes_text_and_uses_irc_line_ending(self) -> None:
        protocol = self.make_protocol()
        protocol.sendLine("PRIVMSG #test :café")
        self.assertEqual(
            cast(StringTransport, protocol.transport).value(),
            b"PRIVMSG #test :caf\xc3\xa9\r\n",
        )

    def test_line_received_decodes_tags_and_configured_encoding(self) -> None:
        protocol = self.make_protocol("latin-1")
        received = []

        def receive(command: str, prefix: str, params: list[str]) -> None:
            received.append((command, prefix, params, protocol._message_tags.copy()))

        protocol.handleCommand = receive  # type: ignore[method-assign]
        protocol.lineReceived(
            b"@account=Alice;example=hello\\sworld :nick!ident@host PRIVMSG #test :caf\xe9"
        )
        self.assertEqual(
            received,
            [
                (
                    "PRIVMSG",
                    "nick!ident@host",
                    ["#test", "café"],
                    {"account": "Alice", "example": "hello world"},
                )
            ],
        )

    def test_sasl_plain_capability_handshake(self) -> None:
        protocol = self.make_protocol()
        protocol.settings.sasl_username = "bot-account"
        protocol.settings.sasl_password = "secret"
        protocol.register("BurlyBot")
        protocol.irc_CAP("server", ["BurlyBot", "LS", "account-tag sasl"])
        protocol.irc_CAP("server", ["BurlyBot", "ACK", "account-tag sasl"])
        protocol.irc_AUTHENTICATE("server", ["+"])

        payload = b64encode(b"\0bot-account\0secret")
        output = cast(StringTransport, protocol.transport).value()
        self.assertIn(b"CAP LS 302\r\n", output)
        self.assertIn(b"CAP REQ :account-tag sasl\r\n", output)
        self.assertIn(b"AUTHENTICATE PLAIN\r\n", output)
        self.assertIn(b"AUTHENTICATE " + payload + b"\r\n", output)

    def test_account_tag_is_attached_to_private_message_event(self) -> None:
        protocol = self.make_protocol()
        protocol.nickname = "BurlyBot"
        protocol.dispatch = Mock()
        protocol._message_tags = {"account": "Alice"}
        protocol.irc_PRIVMSG("nick!ident@host", ["BurlyBot", "!config save"])
        self.assertEqual(protocol.dispatch.call_args.kwargs["account"], "Alice")

    # returns the dispatch Mock separately: protocol.dispatch is declared as a
    # plain Callable, so accessing Mock APIs through it would not type-check
    def make_legacy_protocol(self) -> tuple[BurlyBot, Mock]:
        protocol = self.make_protocol()
        protocol.nickname = "BurlyBot"
        protocol.state = None
        dispatch = Mock()
        protocol.dispatch = dispatch
        protocol.dispatcher = Mock()
        protocol.dispatcher.isAdminCommand = lambda event_type, msg: msg.startswith(
            "!config"
        )
        protocol.register("BurlyBot")
        protocol.irc_CAP("server", ["BurlyBot", "LS", "multi-prefix sasl"])
        cast(StringTransport, protocol.transport).clear()
        return protocol, dispatch

    def test_missing_account_caps_enable_legacy_lookup(self) -> None:
        protocol, _ = self.make_legacy_protocol()
        self.assertTrue(protocol._legacy_account_lookup)

    def test_legacy_admin_command_waits_for_nickserv_status(self) -> None:
        protocol, dispatch = self.make_legacy_protocol()
        protocol.irc_PRIVMSG("Alice!ident@host", ["BurlyBot", "!config save"])
        transport = cast(StringTransport, protocol.transport)
        self.assertIn(b"PRIVMSG NickServ :STATUS Alice\r\n", transport.value())
        dispatch.assert_not_called()

        protocol.irc_NOTICE(
            "NickServ!service@rizon.net", ["BurlyBot", "STATUS Alice 3"]
        )
        privmsg_calls = [
            call for call in dispatch.call_args_list if call.args[1] == "privmsged"
        ]
        self.assertEqual(len(privmsg_calls), 1)
        self.assertEqual(privmsg_calls[0].kwargs["account"], "Alice")
        self.assertEqual(privmsg_calls[0].kwargs["msg"], "!config save")

        # cached: a second admin command dispatches immediately without STATUS
        transport.clear()
        dispatch.reset_mock()
        protocol.irc_PRIVMSG("Alice!ident@host", ["BurlyBot", "!config load"])
        self.assertNotIn(b"STATUS", transport.value())
        self.assertEqual(dispatch.call_args.kwargs["account"], "Alice")

    def test_legacy_status_not_identified_yields_no_account(self) -> None:
        protocol, dispatch = self.make_legacy_protocol()
        protocol.irc_PRIVMSG("Mallory!ident@host", ["BurlyBot", "!config save"])
        protocol.irc_NOTICE(
            "NickServ!service@rizon.net", ["BurlyBot", "STATUS Mallory 1"]
        )
        privmsg_calls = [
            call for call in dispatch.call_args_list if call.args[1] == "privmsged"
        ]
        self.assertEqual(len(privmsg_calls), 1)
        self.assertIsNone(privmsg_calls[0].kwargs["account"])

    def test_legacy_status_ignores_spoofed_reply(self) -> None:
        protocol, dispatch = self.make_legacy_protocol()
        protocol.irc_PRIVMSG("Mallory!ident@host", ["BurlyBot", "!config save"])
        protocol.irc_NOTICE("Mallory!ident@host", ["BurlyBot", "STATUS Mallory 3"])
        self.assertFalse(
            [c for c in dispatch.call_args_list if c.args[1] == "privmsged"]
        )
        protocol._abandonLegacyStatus()

    def test_legacy_lookup_skipped_for_non_admin_commands(self) -> None:
        protocol, dispatch = self.make_legacy_protocol()
        protocol.irc_PRIVMSG("Alice!ident@host", ["BurlyBot", "!help"])
        self.assertNotIn(b"STATUS", cast(StringTransport, protocol.transport).value())
        self.assertIsNone(dispatch.call_args.kwargs["account"])

    def test_legacy_cache_invalidated_on_nick_change(self) -> None:
        protocol, _ = self.make_legacy_protocol()
        protocol.irc_PRIVMSG("Alice!ident@host", ["BurlyBot", "!config save"])
        protocol.irc_NOTICE(
            "NickServ!service@rizon.net", ["BurlyBot", "STATUS Alice 3"]
        )
        protocol.irc_NICK("Alice!ident@host", ["Alicia"])
        cast(StringTransport, protocol.transport).clear()
        protocol.irc_PRIVMSG("Alicia!ident@host", ["BurlyBot", "!config save"])
        self.assertIn(
            b"PRIVMSG NickServ :STATUS Alicia\r\n",
            cast(StringTransport, protocol.transport).value(),
        )
        protocol._abandonLegacyStatus()

    def test_data_received_removes_carriage_return(self) -> None:
        protocol = self.make_protocol()
        received = []
        protocol.handleCommand = lambda command, prefix, params: received.append(params)  # type: ignore[method-assign]
        protocol.dataReceived(b":nick!ident@host PRIVMSG #test :hello\r\n")
        self.assertEqual(received, [["#test", "hello"]])

    def test_unicode_helpers_keep_multibyte_characters_intact(self) -> None:
        self.assertEqual(coerceToUnicode("caf\xe9".encode("latin-1")), "café")
        self.assertEqual(splitEncodedUnicode("a😀b", 5, n=2), [("a😀", 5), ("b", 1)])

    def test_multiline_builder_splits_each_line_once(self) -> None:
        protocol = self.make_protocol()
        protocol.calcAvailableMsgLength = lambda command: 100  # type: ignore[method-assign]
        self.assertEqual(
            protocol._buildmsg("#test", "first\nsecond", split=True),
            ["PRIVMSG #test :first", "PRIVMSG #test :second"],
        )
