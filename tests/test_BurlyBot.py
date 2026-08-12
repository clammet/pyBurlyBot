from typing import Any
from collections import deque
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from twisted.internet.testing import StringTransport

from pyburlybot_modules.steamchat import STEAM_MESSAGE_LIMIT, SteamChat, SteamClientWorker
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
		)

		self.assertEqual(result.returncode, 0, result.stderr)

	def make_protocol(self, encoding: Any="utf-8") -> Any:
		protocol = BurlyBot()
		protocol.settings = SimpleNamespace(encoding=encoding)
		protocol.debug = 0
		protocol.transport = StringTransport()  # type: ignore[assignment]
		protocol._dqueue = deque()
		protocol._lastmsg = 0
		protocol._lines = 0
		protocol._lastCL = None
		return protocol

	def test_send_line_encodes_text_and_uses_irc_line_ending(self) -> None:
		protocol = self.make_protocol()
		protocol.sendLine("PRIVMSG #test :café")

		self.assertEqual(protocol.transport.value(), b"PRIVMSG #test :caf\xc3\xa9\r\n")

	def test_line_received_decodes_with_configured_encoding(self) -> None:
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

	def test_data_received_removes_carriage_return(self) -> None:
		protocol = self.make_protocol()
		received = []
		protocol.handleCommand = lambda command, prefix, params: received.append(params)
		protocol.dataReceived(b":nick!ident@host PRIVMSG #test :hello\r\n")

		self.assertEqual(received, [["#test", "hello"]])

	def test_unicode_helpers_keep_multibyte_characters_intact(self) -> None:
		self.assertEqual(coerceToUnicode("caf\xe9".encode("latin-1")), "café")
		self.assertEqual(
			splitEncodedUnicode("a😀b", 5, n=2),
			[("a😀", 5), ("b", 1)],
		)


class SteamChatTest(TestCase):
	def make_container(self, command_map: Any=None) -> Any:
		return SimpleNamespace(
			network="test",
			state=SimpleNamespace(channels={"#one", "#two"}),
			sendmsg=Mock(),
			_settings=SimpleNamespace(
				dispatcher=SimpleNamespace(
					eventmap={"privmsged": {"command": command_map or {}}},
				),
				setOption=Mock(),
			),
		)

	def make_relay(self, command_map: Any=None, **kwargs: Any) -> SteamChat:
		return SteamChat(
			self.make_container(command_map),
			"!",
			kwargs.pop("allowedmodules", []),
			username="test-user",
			password="test-password",
			login_key="",
			auth_code="",
			credential_directory=Path("data/steamchat/test"),
			autostart=False,
			**kwargs,
		)

	def test_start_and_stop_own_exactly_one_worker(self) -> None:
		workers = []

		class FakeWorker:
			def __init__(self, **kwargs: Any) -> None:
				self.kwargs = kwargs
				self.started = False
				self.stopped = False
				workers.append(self)

			def start(self) -> None:
				self.started = True

			def stop(self) -> None:
				self.stopped = True

		with patch("pyburlybot_modules.steamchat.reactor.callLater") as call_later:
			call_later.return_value = Mock(active=Mock(return_value=False))
			relay = self.make_relay(worker_factory=FakeWorker)  # type: ignore[arg-type]
			relay.start()
			relay.start()
			relay.stop()

		self.assertEqual(len(workers), 1)
		self.assertTrue(workers[0].started)
		self.assertTrue(workers[0].stopped)
		self.assertEqual(workers[0].kwargs["network"], "test")

	def test_command_map_keeps_only_allowed_module_mappings(self) -> None:
		allowed = SimpleNamespace(function=SimpleNamespace(__module__="pyburlybot_modules.allowed"))
		blocked = SimpleNamespace(function=SimpleNamespace(__module__="pyburlybot_modules.blocked"))
		steamchat = self.make_relay(
			{
				"shared": [allowed, blocked],
				"blocked": [blocked, blocked],
			},
			allowedmodules=["allowed"],
		)

		steamchat.populateCommandMap()

		self.assertEqual(steamchat.cmdMap, {"shared": [allowed]})

	def test_irc_relay_does_not_echo_to_its_steam_source_and_batches_output(self) -> None:
		relay = self.make_relay()
		source = relay.getUser("1")
		listener = relay.getUser("2")
		source.channels.add("#one")
		listener.channels.add("#one")
		relay.channels["#one"] = {source, listener}
		worker = Mock()
		worker.send_message.return_value = True
		relay.worker = worker
		relay.connected = True

		with patch("pyburlybot_modules.steamchat.reactor.callLater") as call_later:
			call_later.return_value = Mock(active=Mock(return_value=True))
			relay.ircMSG("#one", "alice", "hello", source)
			relay.steamSay("2", "second")

		self.assertNotIn("1", relay.outbound)
		self.assertEqual(list(relay.outbound["2"]), ["#one <alice> hello", "second"])

		relay._processOutbound()
		worker.send_message.assert_called_once_with("2", "#one <alice> hello\nsecond")
		self.assertFalse(relay.outbound)

	def test_multi_channel_target_without_a_message_is_rejected(self) -> None:
		relay = self.make_relay()
		user = relay.getUser("1")
		user.channels.update(("#one", "#two"))

		with patch("pyburlybot_modules.steamchat.reactor.callLater") as call_later:
			call_later.return_value = Mock(active=Mock(return_value=True))
			relay.steamMSG("1", "#one")

		self.assertEqual(list(relay.outbound["1"]), ["Put a message after the target channel."])

	def test_steam_messages_cannot_inject_an_irc_line(self) -> None:
		relay = self.make_relay()
		user = relay.getUser("1")
		user.name = "Alice"
		user.channels.add("#one")

		relay.steamMSG("1", "hello\r\nMODE #one +o attacker")

		relay.container.sendmsg.assert_called_once_with(
			"#one",
			"<\x02Alice\x02> hello  MODE #one +o attacker",
			steamSource=user,
		)

	def test_outbound_batches_respect_steam_message_limit(self) -> None:
		relay = self.make_relay()
		worker = Mock()
		worker.send_message.return_value = True
		relay.worker = worker
		relay.connected = True
		long_message = "x" * (STEAM_MESSAGE_LIMIT + 25)

		with patch("pyburlybot_modules.steamchat.reactor.callLater") as call_later:
			call_later.return_value = Mock(active=Mock(return_value=True))
			relay.steamSay("1", long_message)

		relay._processOutbound()
		relay._processOutbound()

		self.assertEqual(
			[item.args for item in worker.send_message.call_args_list],
			[("1", "x" * STEAM_MESSAGE_LIMIT), ("1", "x" * 25)],
		)
		self.assertFalse(relay.outbound)

	def test_worker_parses_persona_updates_before_crossing_to_reactor(self) -> None:
		callback = Mock()
		worker = SteamClientWorker(
			network="test",
			username="user",
			password="password",
			login_key="",
			auth_code="",
			credential_directory=Path("data/steamchat/test"),
			callback=callback,
		)
		message = SimpleNamespace(
			body=SimpleNamespace(
				friends=[
					SimpleNamespace(friendid=123, player_name="Alice", persona_state=1),
					SimpleNamespace(friendid=456, player_name="", persona_state=0),
				]
			)
		)

		with patch(
			"pyburlybot_modules.steamchat.reactor.callFromThread",
			side_effect=lambda function, *args: function(*args),
		):
			worker._handle_persona_state(message)

		self.assertEqual(
			[item.args for item in callback.call_args_list],
			[
				("persona", "123", "Alice", True),
				("persona", "456", "456", False),
			],
		)

	def test_worker_stop_wakes_its_gevent_loop(self) -> None:
		worker = SteamClientWorker(
			network="test",
			username="user",
			password="password",
			login_key="",
			auth_code="",
			credential_directory=Path("data/steamchat/test"),
			callback=Mock(),
		)
		worker._hub = SimpleNamespace(loop=Mock())
		worker._spawn = Mock()
		worker._stop_event = Mock()

		worker.stop()

		worker._hub.loop.run_callback_threadsafe.assert_called_once_with(
			worker._spawn,
			worker._stop_event.set,
		)
