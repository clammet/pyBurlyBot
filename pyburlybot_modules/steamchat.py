"""DEPRECATED/abandoned/experimental - Don't use this.

Relay IRC channels through direct messages to a dedicated Steam account.

For a first login protected by Steam Guard, start the bot with a current code in
``PYBURLYBOT_STEAM_AUTH_CODE``. Steam's reusable login key and machine sentry
are persisted after that successful login.
"""

from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from re import sub
from threading import Event as ThreadEvent
from threading import Thread
from traceback import format_tb, print_exc
from typing import Any

from twisted.internet import reactor as _reactor
from twisted.internet.threads import blockingCallFromThread
from twisted.python.failure import Failure
from twisted.words.protocols.irc import CHANNEL_PREFIXES

from util import Mapping, commandSplit, functionHelp
from util.container import Container
from util.event import Event
from util.settings import ConfigException, Settings
from util.types import BotLike

reactor: Any = _reactor

# The HTTP ISteamWebUserPresenceOAuth API used by the old implementation no
# longer exists. SteamClientWorker speaks the Steam Connection Manager protocol
# instead. The worker owns that protocol's gevent hub; the reactor owns all
# relay state below.

FROMIRC_FMT = "%s <%s> %s"
FROMSTEAM_FMT = "<\x02%s\x02> %s"
START_SNOOP = "\x02%s\x02 has \x02started\x02 snooping %s"
STOP_SNOOP = "\x02%s\x02 has \x02stopped\x02 snooping %s"
USER_ITEM = "%s - %s"

OFFLINE_THRESHOLD = 5 * 60
OUTBOUND_INTERVAL = 0.8
RECONNECT_MAX_DELAY = 5 * 60
STEAM_MESSAGE_LIMIT = 2000

OPTIONS = {
	"username": (str, "username of the Steam relay account", ""),
	"password": (str, "password of the Steam relay account", ""),
	"loginKey": (str, "persisted Steam login key; written automatically after login", ""),
	"allowedModules": (
		list,
		'List of modules whose commands may be used from Steam. Only "commands" are loaded.',
		[],
	),
}

COMMAND_PREFIX = None


class SteamIRCBotWrapper:
	"""Restricted bot wrapper used by commands invoked from Steam."""

	def __init__(self, event: Event, botcont: Container, steamchat: SteamChat) -> None:
		self.event = event
		self._botcont = botcont
		self._steamchat = steamchat

	def __getattr__(self, name: str) -> Any:
		return getattr(self._botcont, name)

	def say(self, msg: str, **kwargs: Any) -> None:
		steam_user = self.event.kwargs.get("steamuser")
		if steam_user:
			strins = kwargs.get("strins")
			joinsep = kwargs.get("joinsep")
			if strins:
				if joinsep is not None:
					msg = msg.format(joinsep.join(strins))
				else:
					msg = msg.format(*strins)
			# Module functions run in Twisted's thread pool. Relay state is reactor-owned.
			reactor.callFromThread(self._steamchat.steamSay, steam_user.id, msg)
			return

		dest = self.event.nick or self.event.target
		if not dest:
			raise ValueError("Missing destination in say")
		self.sendmsg(dest, msg, **kwargs)

	def checkSay(self, msg: str, **kwargs: Any) -> bool:
		if self.event.kwargs.get("steamuser"):
			strins = kwargs.get("strins")
			if strins:
				return len(msg) < 1000 and len(strins) < 100
			return len(msg) <= STEAM_MESSAGE_LIMIT
		dest = self.event.target or self.event.nick
		return bool(dest and self._botcont.checkSendMsg(dest, msg))

	def isadmin(self, module: str | None = None) -> bool:
		return False

	def getOption(
		self,
		opt: str,
		channel: str | bool | None = None,
		**kwargs: Any,
	) -> Any:
		return blockingCallFromThread(
			reactor,
			self._botcont._settings.getOption,
			opt,
			channel=channel,
			**kwargs,
		)

	def setOption(
		self,
		opt: str,
		value: Any,
		channel: str | bool | None = None,
		**kwargs: Any,
	) -> None:
		blockingCallFromThread(
			reactor,
			self._botcont._settings.setOption,
			opt,
			value,
			channel=channel,
			**kwargs,
		)

	def _moduleerr(self, error: Any) -> None:
		if not isinstance(error, Failure):
			self.say("Error: %s" % error)
			return

		error.cleanFailure()
		error.printTraceback()
		traceback = error.getTracebackObject()
		exception = error.value
		if traceback:
			location = "| ".join(format_tb(traceback, 5)[-2:]).replace("\n", ". ")
			self.say("%s: %s. %s" % (type(exception).__name__, exception, location))
		else:
			self.say("%s: %s. Check the log for details." % (type(exception).__name__, exception))


SteamEventCallback = Callable[..., None]


class SteamClientWorker(Thread):
	"""Own one Steam client connection and its gevent hub.

	The public methods are safe to call from the reactor thread. No relay data is
	stored here; events are marshalled back to the reactor through ``callback``.
	"""

	def __init__(
		self,
		*,
		network: str,
		username: str,
		password: str,
		login_key: str,
		auth_code: str,
		credential_directory: Path,
		callback: SteamEventCallback,
	) -> None:
		super().__init__(name="SteamClient-%s" % network, daemon=True)
		self.username = username
		self.password = password
		self.login_key = login_key
		self.auth_code = auth_code
		self.credential_directory = credential_directory
		self.callback = callback

		self._stopping = ThreadEvent()
		self._logged_on = ThreadEvent()
		self._hub: Any = None
		self._spawn: Any = None
		self._stop_event: Any = None
		self._client: Any = None
		self._auth_is_2fa: bool | None = None
		self._auth_code_mismatch = False

	def _notify(self, event: str, *args: Any) -> None:
		reactor.callFromThread(self.callback, event, *args)

	def run(self) -> None:
		try:
			from gevent import get_hub, spawn
			from gevent.event import Event as GeventEvent
			from steam.client import SteamClient
			from steam.enums import EResult
			from steam.enums.emsg import EMsg
		except Exception as error:
			self._notify("error", "Unable to load the Steam client: %s" % error)
			self._notify("stopped")
			return

		try:
			self.credential_directory.mkdir(parents=True, exist_ok=True)
			self._hub = get_hub()
			self._spawn = spawn
			self._stop_event = GeventEvent()
			self._client = SteamClient()
			self._client.set_credential_location(str(self.credential_directory))

			self._client.on(self._client.EVENT_LOGGED_ON, self._handle_logged_on)
			self._client.on(self._client.EVENT_DISCONNECTED, self._handle_disconnected)
			self._client.on(self._client.EVENT_AUTH_CODE_REQUIRED, self._handle_auth_required)
			self._client.on(self._client.EVENT_NEW_LOGIN_KEY, self._handle_login_key)
			self._client.on(self._client.EVENT_CHAT_MESSAGE, self._handle_message)
			self._client.on(EMsg.ClientPersonaState, self._handle_persona_state)

			if self._stopping.is_set():
				self._stop_event.set()
			connection = spawn(self._connection_loop, EResult)
			self._stop_event.wait()
			connection.kill()
			if self._client.logged_on:
				self._client.logout()
			elif self._client.connected:
				self._client.disconnect()
		except Exception as error:
			self._notify("error", "Steam worker failed: %s" % error)
			print_exc()
		finally:
			self._logged_on.clear()
			self._notify("stopped")
			if self._hub is not None:
				self._hub.destroy()

	def _connection_loop(self, eresult: Any) -> None:
		delay = 1.0
		pending_auth_code = self.auth_code
		while not self._stopping.is_set():
			self._auth_is_2fa = None
			self._auth_code_mismatch = False
			using_auth_code = False
			login_kwargs: dict[str, Any] = {}
			if self.login_key:
				login_kwargs["login_key"] = self.login_key
			else:
				login_kwargs["password"] = self.password

			result = self._client.login(self.username, **login_kwargs)
			if result == eresult.OK:
				delay = 1.0
				self._client.wait_event(self._client.EVENT_DISCONNECTED)
				continue

			if result == eresult.InvalidPassword and self.login_key:
				self.login_key = ""
				self._notify("login-key", "")
				if not self.password:
					self._notify(
						"error",
						"Steam rejected the persisted login key and no password is configured",
					)
					self._stop_event.wait()
					break
				continue

			if self._auth_is_2fa is not None and pending_auth_code:
				using_auth_code = True
				login_kwargs = {"password": self.password}
				field = "two_factor_code" if self._auth_is_2fa else "auth_code"
				login_kwargs[field] = pending_auth_code
				pending_auth_code = ""
				result = self._client.login(self.username, **login_kwargs)
				if result == eresult.OK:
					delay = 1.0
					self._client.wait_event(self._client.EVENT_DISCONNECTED)
					continue

			if self._auth_is_2fa is not None:
				self._notify(
					"auth-required",
					self._auth_is_2fa,
					self._auth_code_mismatch or using_auth_code,
				)
				self._stop_event.wait()
				break

			self._notify("error", "Steam login failed: %s" % getattr(result, "name", result))
			self._stop_event.wait(timeout=delay)
			delay = min(delay * 2, RECONNECT_MAX_DELAY)

	def _handle_logged_on(self) -> None:
		self._logged_on.set()
		self._notify("logged-on")

	def _handle_disconnected(self) -> None:
		was_logged_on = self._logged_on.is_set()
		self._logged_on.clear()
		if was_logged_on and not self._stopping.is_set():
			self._notify("disconnected")

	def _handle_auth_required(self, is_2fa: bool, code_mismatch: bool) -> None:
		self._auth_is_2fa = is_2fa
		self._auth_code_mismatch = code_mismatch

	def _handle_login_key(self) -> None:
		self.login_key = self._client.login_key or ""
		self._notify("login-key", self.login_key)

	def _handle_message(self, user: Any, message: str) -> None:
		self._notify("message", str(user.steam_id), message)

	def _handle_persona_state(self, message: Any) -> None:
		for friend in message.body.friends:
			self._notify(
				"persona",
				str(friend.friendid),
				friend.player_name or str(friend.friendid),
				bool(friend.persona_state),
			)

	def _schedule(self, function: Callable[..., Any], *args: Any) -> bool:
		if self._hub is None or self._spawn is None or self._stopping.is_set():
			return False
		try:
			self._hub.loop.run_callback_threadsafe(self._spawn, function, *args)
		except Exception:
			return False
		return True

	def send_message(self, user_id: str, message: str) -> bool:
		if not self._logged_on.is_set():
			return False
		return self._schedule(self._send_message, user_id, message)

	def _send_message(self, user_id: str, message: str) -> None:
		try:
			self._client.get_user(user_id, fetch_persona_state=False).send_message(message)
		except Exception as error:
			self._notify("error", "Unable to send Steam message to %s: %s" % (user_id, error))

	def stop(self) -> None:
		self._stopping.set()
		if self._stop_event is not None and self._hub is not None and self._spawn is not None:
			try:
				self._hub.loop.run_callback_threadsafe(
					self._spawn,
					self._stop_event.set,
				)
			except Exception:
				pass


@dataclass(eq=False, slots=True)
class SteamUser:
	id: str
	name: str | None = None
	channels: set[str] = field(default_factory=set)
	offlinetime: float | None = None

	def getName(self) -> str:
		return self.name or self.id


WorkerFactory = Callable[..., SteamClientWorker]


class SteamChat:
	"""Reactor-owned Steam/IRC relay state."""

	def __init__(
		self,
		container: Container,
		cmdprefix: str,
		allowedmodules: Iterable[str],
		*,
		username: str,
		password: str,
		login_key: str,
		auth_code: str,
		credential_directory: Path,
		worker_factory: WorkerFactory = SteamClientWorker,
		autostart: bool = True,
	) -> None:
		self.container = container
		self.cmdprefix = cmdprefix
		self.allowedmodules = set(allowedmodules)
		self.username = username
		self.password = password
		self.login_key = login_key
		self.auth_code = auth_code
		self.credential_directory = credential_directory
		self.worker_factory = worker_factory

		self.users: dict[str, SteamUser] = {}
		self.channels: dict[str, set[SteamUser]] = {}
		self.offlineusers: set[SteamUser] = set()
		self.channelbacklog: dict[str, deque[str]] = {}
		self.cmdMap: dict[str, list[Mapping]] = {}

		self.outbound: OrderedDict[str, deque[str]] = OrderedDict()
		self.worker: SteamClientWorker | None = None
		self.connected = False
		self.status = "not started"
		self._stopped = False
		self._flush_call: Any = None
		self._purge_call: Any = None
		self._startup_call: Any = reactor.callLater(0, self.start) if autostart else None

	def start(self) -> None:
		self._startup_call = None
		if self._stopped or self.worker is not None:
			return
		self.populateCommandMap()
		self.worker = self.worker_factory(
			network=self.container.network,
			username=self.username,
			password=self.password,
			login_key=self.login_key,
			auth_code=self.auth_code,
			credential_directory=self.credential_directory,
			callback=self._handle_worker_event,
		)
		self.auth_code = ""
		self.status = "connecting"
		self.worker.start()
		self._schedule_purge()

	def populateCommandMap(self) -> None:
		command_map = self.container._settings.dispatcher.eventmap.get(
			"privmsged", {}
		).get("command", {})
		self.cmdMap = {}
		for command, mappings in command_map.items():
			allowed = [
				mapping
				for mapping in mappings
				if getattr(mapping.function, "__module__", "").rpartition(".")[2]
				in self.allowedmodules
			]
			if allowed:
				self.cmdMap[command] = allowed

	def _handle_worker_event(self, event: str, *args: Any) -> None:
		if self._stopped:
			return
		if event == "logged-on":
			self.connected = True
			self.status = "connected"
			self._schedule_flush(0)
		elif event == "disconnected":
			self.connected = False
			self.status = "reconnecting"
		elif event == "stopped":
			self.connected = False
			self.status = "stopped"
		elif event == "auth-required":
			is_2fa, mismatch = args
			kind = "two-factor" if is_2fa else "email"
			self.connected = False
			self.status = "%s Steam Guard code required%s; restart with PYBURLYBOT_STEAM_AUTH_CODE set" % (
				kind,
				" (the supplied code was rejected)" if mismatch else "",
			)
			print("STEAMCHAT (%s): %s" % (self.container.network, self.status))
		elif event == "login-key":
			self._save_login_key(args[0])
		elif event == "message":
			self.steamMSG(args[0], args[1])
		elif event == "persona":
			self.steamStatus(args[0], args[1], args[2])
		elif event == "error":
			if not self.connected:
				self.status = args[0]
			print("STEAMCHAT (%s): %s" % (self.container.network, args[0]))

	def _save_login_key(self, login_key: str) -> None:
		if login_key == self.login_key:
			return
		self.login_key = login_key
		self.container._settings.setOption(
			"loginKey", login_key, module="steamchat", channel=False
		)
		Settings.saveOptions()

	def getUser(self, user_id: str) -> SteamUser:
		return self.users.setdefault(user_id, SteamUser(user_id))

	def findUser(self, user: str) -> SteamUser | None:
		if user in self.users:
			return self.users[user]
		return next((item for item in self.users.values() if item.name == user), None)

	def _schedule_purge(self) -> None:
		if not self._stopped:
			self._purge_call = reactor.callLater(30, self._purge_and_reschedule)

	def _purge_and_reschedule(self) -> None:
		self._purge_call = None
		self.purgeOffline()
		self._schedule_purge()

	def purgeOffline(self) -> None:
		from time import time

		now = time()
		for user in list(self.offlineusers):
			if user.offlinetime is None or now <= user.offlinetime + OFFLINE_THRESHOLD:
				continue
			self.offlineusers.remove(user)
			for channel in list(user.channels):
				self.removeUserFromChannel(user, channel)

	def removeUserFromChannel(
		self,
		user: SteamUser,
		channel: str,
		*,
		sayIRC: bool = True,
	) -> None:
		listeners = self.channels.get(channel)
		if listeners is not None:
			listeners.discard(user)
			if not listeners:
				self.channels.pop(channel, None)
		user.channels.discard(channel)
		if sayIRC:
			self.ircSay(channel, STOP_SNOOP % (user.getName(), channel))
		self.steamSay(user.id, "Stopped listening to %s." % channel)

	def ircSay(self, channel: str, msg: str, source: SteamUser | None = None) -> None:
		if source:
			msg = FROMSTEAM_FMT % (source.getName(), msg)
		self.container.sendmsg(channel, msg, steamSource=source)

	def ircMSG(
		self,
		channel: str,
		nick: str,
		msg: str,
		steamSource: SteamUser | None = None,
	) -> None:
		formatted = FROMIRC_FMT % (channel, nick, msg)
		self.channelbacklog.setdefault(channel, deque(maxlen=5)).append(formatted)
		for user in self.channels.get(channel, set()):
			if steamSource is None or user.id != steamSource.id:
				self.steamSay(user.id, formatted)

	def steamCMD(self, sourceid: str, msg: str) -> None:
		command, argument = commandSplit(msg)
		if command is None:
			return
		command = command[len(self.cmdprefix) :].lower()
		user = self.getUser(sourceid)

		if command == "listen":
			if not argument:
				if not user.channels:
					self.steamSay(sourceid, 'Not listening to any channels. Use "listen <#channel>".')
				else:
					self.steamSay(sourceid, "Listening to: %s" % ", ".join(sorted(user.channels)))
				return
			if argument not in self.container.state.channels:
				self.steamSay(sourceid, "Can't listen to a channel I'm not in.")
				return
			if argument in user.channels:
				self.steamSay(sourceid, "Already listening to (%s)." % argument)
				return
			user.channels.add(argument)
			self.channels.setdefault(argument, set()).add(user)
			self.ircSay(argument, START_SNOOP % (user.getName(), argument))
			self.steamSay(sourceid, "Listening to (%s)." % argument)
			backlog = self.channelbacklog.get(argument)
			if backlog:
				self.steamSay(sourceid, "\n".join(backlog))
			return

		if command == "leave":
			if not user.channels:
				self.steamSay(sourceid, "Not listening to any channels.")
				return
			if not argument:
				if len(user.channels) != 1:
					self.steamSay(
						sourceid,
						"Choose a channel to leave: %s" % ", ".join(sorted(user.channels)),
					)
					return
				argument = next(iter(user.channels))
			if argument not in user.channels:
				self.steamSay(sourceid, "You aren't listening to that channel.")
				return
			self.removeUserFromChannel(user, argument)
			return

		if command in {"quit", "stop"}:
			if not user.channels:
				self.steamSay(sourceid, "You weren't listening to any channels. Bye.")
				return
			for channel in list(user.channels):
				self.removeUserFromChannel(user, channel)
			self.steamSay(sourceid, "Bye.")
			return

		if command == "help":
			self.steamSay(
				sourceid,
				'Use "%slisten <#channel>" and "%sleave [#channel]" to manage relays. '
				'When listening to several channels, prefix messages with the target channel. '
				'Use "%shhelp" for the bot command list.'
				% (self.cmdprefix, self.cmdprefix, self.cmdprefix),
			)
			return

		if command == "hhelp":
			command = "help"

		mappings = self.cmdMap.get(command, ())
		if not mappings:
			self.steamSay(sourceid, "Unknown command. Use %shelp." % self.cmdprefix)
			return
		event = Event(
			None,
			nick=user.getName(),
			command=command,
			argument=argument,
			steamuser=user,
		)
		wrapper = SteamIRCBotWrapper(event, self.container, self)
		for mapping in mappings:
			self.container._settings.dispatcher._dispatchreally(
				mapping.function,
				event,
				wrapper,
				self.container._settings.debug,
			)
			if mapping.priority == 0:
				break

	def steamMSG(self, sourceid: str, msg: str) -> None:
		msg = msg.replace("\r", " ").replace("\n", " ").strip()
		if not msg:
			return
		if msg.startswith(self.cmdprefix):
			self.steamCMD(sourceid, msg)
			return

		user = self.users.get(sourceid)
		if user is None or not user.channels:
			self.steamSay(sourceid, 'Use "%slisten <#channel>" before relaying messages.' % self.cmdprefix)
			return

		if len(user.channels) == 1:
			channel = next(iter(user.channels))
			body = msg
		else:
			if msg[0] not in CHANNEL_PREFIXES:
				self.steamSay(
					sourceid,
					"Listening to several channels (%s); prefix the message with its target."
					% ", ".join(sorted(user.channels)),
				)
				return
			channel, separator, body = msg.partition(" ")
			if not separator or not body:
				self.steamSay(sourceid, "Put a message after the target channel.")
				return
			if channel not in user.channels:
				self.steamSay(sourceid, "You aren't listening to that channel.")
				return

		if channel not in self.container.state.channels:
			self.removeUserFromChannel(user, channel, sayIRC=False)
			self.steamSay(sourceid, "I'm no longer in %s, so that relay was removed." % channel)
			return
		self.ircSay(channel, body, user)

	def steamStatus(self, sourceid: str, name: str, online: bool) -> None:
		from time import time

		user = self.getUser(sourceid)
		user.name = name.replace("\r", " ").replace("\n", " ")
		if online:
			user.offlinetime = None
			self.offlineusers.discard(user)
		else:
			user.offlinetime = time()
			self.offlineusers.add(user)

	def steamSay(self, userid: str, msg: str) -> None:
		self.outbound.setdefault(userid, deque(maxlen=10)).append(msg)
		self._schedule_flush(OUTBOUND_INTERVAL)

	def _schedule_flush(self, delay: float) -> None:
		if self._stopped or self._flush_call is not None or not self.outbound:
			return
		self._flush_call = reactor.callLater(delay, self._processOutbound)

	def _processOutbound(self) -> None:
		self._flush_call = None
		if self._stopped or not self.outbound:
			return
		if not self.connected or self.worker is None:
			self._schedule_flush(1.0)
			return

		userid = next(iter(self.outbound))
		messages = self.outbound[userid]
		text, consumed, remainder = self._build_outbound_batch(messages)
		if self.worker.send_message(userid, text):
			for _ in range(consumed):
				messages.popleft()
			if remainder is not None:
				messages[0] = remainder
			if messages:
				self.outbound.move_to_end(userid)
			else:
				self.outbound.pop(userid)
		self._schedule_flush(OUTBOUND_INTERVAL)

	@staticmethod
	def _build_outbound_batch(messages: deque[str]) -> tuple[str, int, str | None]:
		parts: list[str] = []
		consumed = 0
		remainder = None
		for message in messages:
			separator_length = 1 if parts else 0
			available = STEAM_MESSAGE_LIMIT - len("\n".join(parts)) - separator_length
			if len(message) <= available:
				parts.append(message)
				consumed += 1
				continue
			if available:
				parts.append(message[:available])
				remainder = message[available:]
			break
		return "\n".join(parts), consumed, remainder

	def listUsers(self, dest: str) -> None:
		users = self.channels.get(dest)
		if not users:
			self.ircSay(dest, "No one is listening in here.")
			return
		items = sorted(USER_ITEM % (user.id, user.getName()) for user in users)
		self.ircSay(dest, "Users listening to (%s): %s" % (dest, ", ".join(items)))

	def reportStatus(self, bot: BotLike) -> None:
		bot.say("Steam relay: %s." % self.status)

	def leftIRCChannel(self, channel: str) -> None:
		for user in list(self.channels.get(channel, ())):
			self.removeUserFromChannel(user, channel, sayIRC=False)

	def kickUser(self, target: str, user: str) -> None:
		steam_user = self.findUser(user)
		if steam_user is None:
			self.ircSay(target, "Don't know (%s)." % user)
		elif target not in steam_user.channels:
			self.ircSay(target, "(%s) isn't listening in here." % steam_user.getName())
		else:
			self.removeUserFromChannel(steam_user, target)

	def fromIRC(self, function: str, *args: Any) -> None:
		method = getattr(self, function, None)
		if method is None or function.startswith("_"):
			raise ValueError("Unknown Steam relay operation: %s" % function)
		reactor.callFromThread(method, *args)

	def stop(self) -> None:
		if self._stopped:
			return
		self._stopped = True
		self.connected = False
		self.status = "stopping"
		for delayed_call in (self._startup_call, self._flush_call, self._purge_call):
			if delayed_call is not None and delayed_call.active():
				delayed_call.cancel()
		self._startup_call = self._flush_call = self._purge_call = None
		if self.worker is not None:
			self.worker.stop()


STEAM_RELAYS: dict[str, SteamChat] = {}


def steamchatcmd(event: Event, bot: BotLike) -> None:
	"""steamchat [status|kick user]. Show or manage the Steam relay."""
	relay = STEAM_RELAYS.get(bot.network)
	if relay is None:
		bot.say("Error: No Steam relay is available for this network.")
		return
	if not event.argument:
		relay.fromIRC("listUsers", event.nick if event.isPM() else event.target)
		return
	command, argument = commandSplit(event.argument)
	if command == "status" and not argument:
		relay.fromIRC("reportStatus", bot)
	elif command == "kick" and argument:
		relay.fromIRC("kickUser", event.nick if event.isPM() else event.target, argument)
	else:
		bot.say(functionHelp(steamchatcmd))


def doleft(event: Event, bot: BotLike) -> None:
	relay = STEAM_RELAYS.get(bot.network)
	if relay:
		relay.fromIRC("leftIRCChannel", event.target)


def relaymsg(event: Event, bot: BotLike) -> None:
	relay = STEAM_RELAYS.get(bot.network)
	if relay and not event.isPM() and event.msg is not None:
		relay.fromIRC("ircMSG", event.target, event.nick, event.msg)


def processBotSendmsg(event: Event, bot: BotLike) -> None:
	# Send-message hooks must never break the bot's IRC output path.
	try:
		relay = STEAM_RELAYS.get(bot.network)
		if relay is None or event.isPM() or event.msg is None:
			return
		msg = event.msg
		strins = event.kwargs.get("strins")
		if strins:
			joinsep = event.kwargs.get("joinsep")
			msg = msg.format(joinsep.join(strins)) if joinsep is not None else msg.format(*strins)
		relay.fromIRC(
			"ircMSG",
			event.target,
			event.nick,
			msg,
			event.kwargs.get("steamSource"),
		)
	except Exception:
		print("Steam sendmsg relay failed")
		print_exc()


def init(bot: BotLike) -> bool:
	if not bot.getOption("enablestate"):
		raise ConfigException('steamchat module requires the "enablestate" option')
	if bot.network in STEAM_RELAYS:
		raise ConfigException("A Steam relay already exists for network %s" % bot.network)

	username = bot.getOption("username", module="steamchat")
	password = bot.getOption("password", module="steamchat")
	login_key = bot.getOption("loginKey", module="steamchat")
	if not username:
		raise ConfigException('steamchat requires a "username"')
	if not password and not login_key:
		raise ConfigException('steamchat requires a "password" or persisted "loginKey"')

	safe_network = sub(r"[^A-Za-z0-9_.-]", "_", bot.network)
	credential_directory = Path(bot.getOption("datadir")) / "steamchat" / safe_network
	STEAM_RELAYS[bot.network] = SteamChat(
		bot.container,
		bot.getOption("commandprefix"),
		bot.getOption("allowedModules", module="steamchat"),
		username=username,
		password=password,
		login_key=login_key,
		auth_code=environ.get("PYBURLYBOT_STEAM_AUTH_CODE", ""),
		credential_directory=credential_directory,
	)
	return True


def unload() -> None:
	for relay in STEAM_RELAYS.values():
		relay.stop()
	STEAM_RELAYS.clear()


mappings = (
	Mapping(types=["privmsged"], function=relaymsg),
	Mapping(types=("kickedFrom", "left"), function=doleft),
	Mapping(command=("steamchat", "sc"), function=steamchatcmd),
	Mapping(["sendmsg"], function=processBotSendmsg),
)
