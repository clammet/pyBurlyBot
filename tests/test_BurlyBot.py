from collections import deque
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from twisted.internet.testing import StringTransport

from pyburlybot_modules.steamchat import SteamChat
from util.client import BurlyBot
from util.helpers import coerceToUnicode, splitEncodedUnicode


class BurlyBotProtocolTest(TestCase):
	def test_logging_allows_multiprocessing_to_start(self):
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
		)

		self.assertEqual(result.returncode, 0, result.stderr)

	def make_protocol(self, encoding="utf-8"):
		protocol = BurlyBot()
		protocol.settings = SimpleNamespace(encoding=encoding)
		protocol.debug = 0
		protocol.transport = StringTransport()
		protocol._dqueue = deque()
		protocol._lastmsg = 0
		protocol._lines = 0
		protocol._lastCL = None
		return protocol

	def test_send_line_encodes_text_and_uses_irc_line_ending(self):
		protocol = self.make_protocol()
		protocol.sendLine("PRIVMSG #test :café")

		self.assertEqual(protocol.transport.value(), b"PRIVMSG #test :caf\xc3\xa9\r\n")

	def test_line_received_decodes_with_configured_encoding(self):
		protocol = self.make_protocol("latin-1")
		received = []
		protocol.handleCommand = lambda command, prefix, params: received.append(
			(command, prefix, params)
		)

		protocol.lineReceived(b":nick!ident@host PRIVMSG #test :caf\xe9")

		self.assertEqual(
			received,
			[("PRIVMSG", "nick!ident@host", ["#test", "café"])],
		)

	def test_data_received_removes_carriage_return(self):
		protocol = self.make_protocol()
		received = []
		protocol.handleCommand = lambda command, prefix, params: received.append(params)
		protocol.dataReceived(b":nick!ident@host PRIVMSG #test :hello\r\n")

		self.assertEqual(received, [["#test", "hello"]])

	def test_unicode_helpers_keep_multibyte_characters_intact(self):
		self.assertEqual(coerceToUnicode("caf\xe9".encode("latin-1")), "café")
		self.assertEqual(
			splitEncodedUnicode("a😀b", 5, n=2),
			[("a😀", 5), ("b", 1)],
		)


class SteamChatTest(TestCase):
	def make_container(self, command_map=None):
		def get_option(option, **kwargs):
			return "test-token"

		return SimpleNamespace(
			network="test",
			getOption=get_option,
			_settings=SimpleNamespace(
				dispatcher=SimpleNamespace(
					eventmap={"privmsged": {"command": command_map or {}}},
				),
			),
		)

	def test_initialization_reads_oauth_through_container_api(self):
		calls = []

		def get_option(option, **kwargs):
			calls.append((option, kwargs))
			return "test-token"

		container = SimpleNamespace(network="test", getOption=get_option)
		with patch("pyburlybot_modules.steamchat.reactor.callFromThread"):
			steamchat = SteamChat(container, "!", [])

		self.assertEqual(steamchat.oauth, "test-token")
		self.assertEqual(
			calls,
			[("oauthtoken", {"module": "steamchat", "inreactor": True})],
		)

	def test_command_map_keeps_only_allowed_module_mappings(self):
		allowed = SimpleNamespace(function=SimpleNamespace(__module__="pyburlybot_modules.allowed"))
		blocked = SimpleNamespace(function=SimpleNamespace(__module__="pyburlybot_modules.blocked"))
		container = self.make_container(
			{
				"shared": [allowed, blocked],
				"blocked": [blocked, blocked],
			}
		)
		with patch("pyburlybot_modules.steamchat.reactor.callFromThread"):
			steamchat = SteamChat(container, "!", ["allowed"])

		steamchat.populateCommandMap()

		self.assertEqual(steamchat.cmdMap, {"shared": [allowed]})
