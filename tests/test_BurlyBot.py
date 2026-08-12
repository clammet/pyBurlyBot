from collections import deque
from types import SimpleNamespace
from unittest import TestCase

from twisted.internet.testing import StringTransport

from util.client import BurlyBot
from util.helpers import coerceToUnicode, splitEncodedUnicode


class BurlyBotProtocolTest(TestCase):
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
