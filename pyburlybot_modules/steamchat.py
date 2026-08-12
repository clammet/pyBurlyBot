from collections.abc import Iterable
from typing import Any
from util.types import BotLike
### IN DEVELOPMENT 
# Cool Steamchat module. Allows relaying between IRC<->Steam, and allows usage of module commands from Steam!
from json import loads
from base64 import b64encode
from urllib.parse import urlencode
from time import sleep, time
from threading import Thread
from queue import Queue, Empty
from collections import deque, OrderedDict
from traceback import print_exc, format_tb

from twisted.words.protocols.irc import CHANNEL_PREFIXES
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread
from twisted.python.failure import Failure

from requests import Session
from requests.exceptions import ConnectionError, HTTPError
from rsa import PublicKey, encrypt

from util.settings import ConfigException, Settings
from util.event import Event
from util import Mapping, commandSplit, functionHelp, pastehelper
from util.container import Container

# SMALL TODO:
#	Put outbound in own thread
#	check friend things

RSAKEY_URL = "https://steamcommunity.com/login/getrsakey?%s"
# Need to use mobile login URL because normal one doesn't have an API access token in it...
# Normal one: "https://steamcommunity.com/login/dologin"
LOGIN_URL = "https://steamcommunity.com/mobilelogin/dologin/"
# Combined with mobile login page we need to use an oauth client ID which people call a "UNIVERSE"
# Public DE45CD61  taken from here: https://bitbucket.org/Aerizeon/steamweb
LOGIN_CLIENT_ID = "DE45CD61"
CAPTCHA_URL = "https://steamcommunity.com/public/captcha.php?%s" #gid=
CHAT_LOGIN_URL = "https://api.steampowered.com/ISteamWebUserPresenceOAuth/Logon/v0001"
CHAT_LOGOUT_URL = "https://api.steampowered.com/ISteamWebUserPresenceOAuth/Logoff/v0001"
POLLER_URL = "https://api.steampowered.com/ISteamWebUserPresenceOAuth/Poll/v0001"

SENDMSG_URL = "https://api.steampowered.com/ISteamWebUserPresenceOAuth/Message/v0001"

#channel <nick> msg
FROMIRC_FMT = "%s <%s> %s"
FROMSTEAM_FMT = "<\x02%s\x02> %s"
START_SNOOP = "\x02%s\x02 has \x02started\x02 snopping %s"
STOP_SNOOP = "\x02%s\x02 has \x02stopped\x02 snopping %s"

USER_ITEM = "%s - %s"

OPTIONS = {
	"username" : (str, "username of steam account", ""),
	"password" : (str, "password of steam account", ""),
	"oauthtoken" : (str, "oauth token for later use", ""),
	"allowedModules" : (list, 'List of modules which commands can be used from. Only "commands" will be loaded.', []),
}

COMMAND_PREFIX = None

# keep assuming user is 'online' (and continue delivering messages on listened channels) until this threshold is passed
OFFLINE_THRESHOLD = 5*60

#SteamChat cmdqueue.put ("COMMAND", ARGS) where COMMAND is a functionname of SteamChat 
#	and ARGS is a tuple/list of arguments to be passed to that function

COOLDOWN = 15*60 #hour

class SteamIRCBotWrapper:
	""" Taken mostly from util.wrapper """
	def __init__(self, event: Event, botcont: Container, steamchat: SteamChat) -> None:
		self.event = event
		self._botcont = botcont
		self._steamchat = steamchat
		
	def __getattr__(self, name: str) -> Any:
		if name in self.__dict__: return getattr(self, name)
		return getattr(self._botcont, name)
	
	def say(self, msg: str, **kwargs: Any) -> None:
		print(repr(msg), kwargs)
		su = self.event.kwargs.get('steamuser')
		if su:
			strins = kwargs.get("strins")
			joinsep = kwargs.get("joinsep")
			if strins:
				if joinsep is not None: msg = msg.format(joinsep.join(strins))
				else: msg = str(msg).format(*strins)
			self._steamchat.steamSay(su.id, msg)
		else:
			dest = self.event.nick if self.event.nick else self.event.target
			if not dest:
				raise ValueError("Missing dest in say")
			self.sendmsg(dest, msg, **kwargs)
	
	def checkSay(self, msg: str, **kwargs: Any) -> bool:
		su = self.event.kwargs.get('steamuser')
		if su:
			strins = kwargs.get("strins")
			if strins and len(strins) < 100 and len(msg) < 1000:
				return True
			else:
				return len(msg) < 2500
		else:
			if self.event.target:
				return self._botcont.checkSendMsg(self.event.target, msg)
			else:
				return self._botcont.checkSendMsg(self.event.nick, msg)
	
	def isadmin(self, module: str | None=None) -> bool:
		return False
		
	def getOption(self, opt: str, channel: str | bool | None=None,
		**kwargs: Any) -> Any:
		return blockingCallFromThread(reactor, self._botcont._settings.getOption, opt, channel=channel, **kwargs)
	def setOption(self, opt: str, value: Any, channel: str | bool | None=None,
		**kwargs: Any) -> None:
		blockingCallFromThread(reactor, self._botcont._settings.setOption, opt, value, channel=channel, **kwargs)

	#callback to handle module errors
	def _moduleerr(self, e: Any) -> None:
		if isinstance(e, Failure):
			e.cleanFailure()
			e.printTraceback()
			tb = e.getTracebackObject()
			ex = e.value
			if tb:
				# The (hopefully) most 2 important stacks from the traceback.
				# The first 2 are from twisted, the next one is the module stack, probably, and then the next one is whatever the
				# module called.
				self.say("%s: %s. %s" % (type(ex).__name__, ex, "| ".join(format_tb(tb, 5)[-2:]).replace("\n", ". ")))
			else:
				self.say("%s: %s. Don't know where, check log." % (type(ex).__name__, ex))
		else:
			self.say("Error: %s" % str(e))
			print("error:", e)

class SteamPoller(Thread):
	def __init__(self, inq: Queue[tuple[str, tuple[Any, ...]]], accesstoken: str,
		umqid: str, msgid: str | int) -> None:
		super().__init__()
		self.steamchatq = inq
		self.pollerq: Queue[str] = Queue()
		self.umqid = umqid
		self.accesstoken = accesstoken
		self.msgid = msgid
		self.session = Session()
		
	def run(self) -> None:
		pollid = 0
		d = {"access_token" : self.accesstoken, "umqid" : self.umqid, "message" : self.msgid, 
			"pollid" : pollid, "sectimeout" : 20, "secidletime" : 10, "use_accountids" : 0}
		while True:
			try: item = self.pollerq.get(False) # Don't block on this, only on urlopen
			except Empty: pass
			else: 
				if item == "QUIT": break
			#else continue with long GET
			rdata = self.session.post(POLLER_URL, d, timeout=22.0).json()
			d['message'] = rdata.get('messagelast', d['message'])
			err = rdata['error']
			if err == "OK":
				for message in rdata['messages']:
					t = message['type']
					mfrom = message['steamid_from']
					if t == "personastate":
						# track online/offline and usernames
						online = False if message.get('persona_state', 0) == 0 else True
						self.steamchatq.put(("steamStatus", (mfrom, message['persona_name'], online)))
					elif t == "saytext":
						#recv msg
						self.steamchatq.put(("steamMSG", (mfrom, message['text'])))
			elif err == "Not Logged On": 
				self.steamchatq.put(("steamDC", ()))
				break
			elif err != "Timeout": print("===========WAT HAPEN? (%s)===========\n%s" % (err, rdata))
			pollid += 1
		print("SHUT DOWN STEAMPOLLER")
	
	def stop(self) -> None:
		self.pollerq.put("QUIT")

class SteamUser:
	def __init__(self, id: str, name: str | None=None) -> None:
		self.id = id
		self.name = name
		self.channels: set[str] = set()
		self.offlinetime: float | None = None
		
	def getName(self) -> str:
		return self.name if self.name else self.id

# Steam thread
class SteamChat(Thread):
	def __init__(self, container: Container, cmdprefix: str,
		allowedmodules: Iterable[str]) -> None:
		super().__init__()
		self.cmdQueue: Queue[tuple[str, tuple[Any, ...]]] = Queue()
		self.name = "SteamChatThread-%s" % container.network
		self.container = container
		self.cmdprefix = cmdprefix
		self.online = False
		self.cooldownuntil = 0.0
		self.oauth = self.container.getOption("oauthtoken", module="steamchat", inreactor=True)
		# users their friendly name, last offline time and their channels
		# offline time for allowing users to disconnect/reconnect and still keep listened channels
		self.users: dict[str, SteamUser] = {} # {userid : SteamUser}
		
		self.channels: dict[str, set[SteamUser]] = {} # reverse mapping of the above {channel : set(users)}
		self.offlineusers: set[SteamUser] = set() # for easy checking of temporary offline users
		self.poller: SteamPoller | None = None
		self.sendready = False
		self.senddict: dict[str, Any] = {}
		
		self.cmdMap: dict[str, list[Mapping]] = {}
		self.allowedmodules = allowedmodules
		# populate command map after dispatcher has finished loading
		reactor.callFromThread(reactor.callLater, 22.0, self.populateCommandMap)
		# start thread later so that previous instances have time to unload
		reactor.callFromThread(reactor.callLater, 24.0, self.start)
		self.outbound: OrderedDict[str, deque[str]] = OrderedDict() # user : deque
		self.lastout = time()
		self.doout = False
		self.session = Session()
		self.channelbacklog: dict[str, deque[str]] = {}
	
	def populateCommandMap(self) -> None:
		# command map. SOMETHING LIKE THIS SHOULD NEVER BE DONE. GOSH.
		command_map = self.container._settings.dispatcher.eventmap.get("privmsged", {}).get("command", {})
		self.cmdMap = {}
		for cmd, mappings in command_map.items():
			allowed = [
				mapping for mapping in mappings
				if getattr(mapping.function, "__module__", "").rpartition(".")[2] in self.allowedmodules
			]
			if allowed:
				self.cmdMap[cmd] = allowed
	
	def run(self) -> None:
		self.login()
		t = time()
		while True:
			if (not self.oauth) or (not self.sendready): #require missing oauth or missing sendready before attempt connect
				if time() > self.cooldownuntil:
					self.login()
				else:
					sleep(0.5)
			#process queue
			try: cmd, args = self.cmdQueue.get(False) # Don't block
			except Empty: pass
			else:
				#process queue item
				print("PROCESSING... %s(%s)" % (cmd, args))
				if cmd == "QUIT": break
				else:
					# attempt to dispatch to method
					try: getattr(self, cmd)(*args)
					except Exception:
						print("ERROR IN STEAMCHAT LOOP STEAMCHAT FUNC:")
						print_exc()
						
			try: self.purgeOffline()
			except Exception:
				print("ERROR in purgeOffline():")
				print_exc()
			if self.doout and time() > (t + 0.8): # 0.8 arbitrary delay
				# process outbound messages
				try: self._processOutbound()
				except Exception:
					print("ERROR in _processOutbound:")
					print_exc()
				t = time()
			sleep(0.1)

		#clean up (shut down poller)
		logout = self.sendready
		sd = self.senddict.copy()
		self.checkAndStopPoll()
		if logout and sd['umqid']:
			sd.pop("type")
			r = self.session.post(CHAT_LOGOUT_URL, sd)
			try:
				r.raise_for_status()
			except HTTPError:
				print("Exception when attempting logout:")
				print_exc()
			else:
				print("LOGGED OUT OF STEAM")
	
	def getUser(self, uid: str) -> SteamUser:
		return self.users.setdefault(uid, SteamUser(uid))
		
	def findUser(self, user: str) -> SteamUser | None:
		if user in self.users: return self.users[user]
		else:
			for u in self.users.values():
				if u.name == user: return u
		return None
	
	def steamDC(self) -> None:
		# when steam disconnects me, what do (will happen when I request disconnection, 
		# but this won't have a chance to be called by then because we aren't in the loop anymore.)
		# I guess we mimick checkAndStopPoll, without the poll stuff
		self.poller = None
		self.sendready = False
		self.senddict.clear()
		self.cooldownuntil = time() + COOLDOWN
	
	def purgeOffline(self) -> None:
		t = time()
		for user in list(self.offlineusers):
			if user.offlinetime is not None and t > user.offlinetime + OFFLINE_THRESHOLD:
				self.offlineusers.remove(user)
				for chan in list(user.channels):
					self.removeUserFromChannel(user, chan)
				
	def removeUserFromChannel(self, user: SteamUser, channel: str,
		sayIRC: bool=True) -> None:
		self.channels[channel].remove(user)
		if sayIRC: self.ircSay(channel, STOP_SNOOP % (user.getName(), channel))
		self.steamSay(user.id, "Stopped listening to %s." % channel)
		user.channels.remove(channel)
		
	def ircSay(self, channel: str, msg: str, source: SteamUser | None=None) -> None:
		if source:
			msg = FROMSTEAM_FMT % (source.getName(), msg)
		self.container.sendmsg(channel, msg, steamSource=source)
	
	def ircMSG(self, channel: str, nick: str, msg: str,
		steamSource: SteamUser | None=None) -> None:
		msg = FROMIRC_FMT % (channel, nick, msg)
		self.channelbacklog.setdefault(channel, deque(maxlen=5)).append(msg)
		users: Iterable[SteamUser] = self.channels.get(channel, set())
		if users:
			for user in users:
				if user.getName() != steamSource:
					self.steamSay(user.id, msg)
	
	#handle steam command
	def steamCMD(self, sourceid: str, msg: str) -> None:
		#stolen from dispatcher
		command, argument = commandSplit(msg)
		if command is None:
			return
		command = command[len(self.cmdprefix):].lower()
		u = self.getUser(sourceid)
		# TODO: Someone should clean this up a bit... probably.
		if command == "listen":
			if not argument:
				if not u or not u.channels: return self.steamSay(sourceid, 'Not listening to any channels. Type "listen <#channelname>" to start snooping.')
				else: return self.steamSay(sourceid, "Listening to:%s\n" % "\n".join(u.channels))
			else:
				if argument not in self.container.state.channels:
					return self.steamSay(sourceid, "Can't listen to channel I'm not in.")
				#else listen to channel
				else:
					u.channels.add(argument)
					self.channels.setdefault(argument, set()).add(u)
					self.ircSay(argument, START_SNOOP % (u.getName(), argument))
					self.steamSay(sourceid, "Listening to (%s)" % argument)
					backlog: Iterable[str] = self.channelbacklog.get(argument, ())
					if backlog: self.steamSay(sourceid, "\n".join(backlog))
					return
		elif command == "leave":
			if not argument:
				if not u or not u.channels: return self.steamSay(sourceid, 'Not listening to any channels. Type "listen <#channelname>" to start snooping.\n'
					'and "leave <#channelname>" to leave "channelname", or just "leave" if you are only in a single channel.')
				else:
					if len(u.channels) == 1:
						return self.removeUserFromChannel(u, next(iter(u.channels))) # to get item without .pop().next()
					else:
						return self.steamSay(sourceid, "I need to know what channel you want to stop listening to. You are listening to: (%s)."
							'Use "leave #channelname" to leave the channel "channelname"' % ", ".join(u.channels))
			else:
				if not u or not u.channels: self.steamSay(sourceid, 'Not listening to any channels. Type "listen <#channelname>" to start snooping.\n'
					'and "leave <#channelname>" to leave "channelname", or just "leave" if you are only in a single channel.')
				else:
					if argument not in u.channels:
						return self.steamSay(sourceid, "You aren't listening to that channel. You are listening to: (%s)." % ", ".join(u.channels))
					else:
						return self.removeUserFromChannel(u, argument)
		elif command == "quit" or command == "stop":
			if not u or not u.channels: return self.steamSay(sourceid, "You weren't listening to any channels. Bye bye.")
			else:
				for c in list(u.channels):
					self.removeUserFromChannel(u, c)
				self.steamSay(sourceid, "Bye bye.")
		elif command == "help":
			return self.steamSay(sourceid, 'Use "listen" to join channels. Type messages to me to relay them to a channel.\n'
				'If you are listening to multiple channels you need to prefix the target channel in your message e.g. "#channel hello".\n'
				'Use "leave" to stop listening to a channel. Use "quit" or "stop" to stop listening to all channels.\n'
				'To use my normal "help" function, use "hhelp". (Doesn\'t work yet...)')
		elif command == "hhelp":
			msg.replace("hhelp", "help", 1)
		
		cont_or_wrap = None
		u = self.getUser(sourceid)
		event = None
		for mapping in self.cmdMap.get(command,()):
			if not event: event = Event(None, nick=u.getName(), command=command, argument=argument, steamuser=u)
			if not cont_or_wrap: cont_or_wrap = SteamIRCBotWrapper(event, self.container, self) # event, botcont, steamchat
			# massive silliness
			reactor.callFromThread(self.container._settings.dispatcher._dispatchreally,
				mapping.function, event, cont_or_wrap, self.container._settings.debug)
			if mapping.priority == 0: break
	
	# handle messages from Steam here. Includes commands and such
	def steamMSG(self, sourceid: str, msg: str) -> None:
		msg = msg.replace("\n", " ")
		if msg.startswith(self.cmdprefix):
			# process command
			return self.steamCMD(sourceid, msg)
			# if attempting to join a channel, check if bot is actually in it using container.state
		else:
			# process chat message
			user = self.users.get(sourceid)
			if not user: 
				return self.steamSay(sourceid, "Weird that I don't know you, "
					"but you need to be listening to channels before sending to them. Try using listen.")
			channels = user.channels
			if not channels:
				self.steamSay(sourceid, "Need to be listening to channel(s) to send to them. Try using listen.")
			else:
				if len(channels) == 1:
					channel = next(iter(user.channels)) # bit silly to just get the only item without pop().add()
					if channel not in self.container.state.channels:
						#remove listen channel and give message
						channels.remove(channel)
						self.steamSay(sourceid, "I'm not in %s for some reason, "
							"so you can't send to it and won't be receiving messages from it." % channel)
					else:
						self.ircSay(channel, msg, user)
				else:
					# check for channel prefix
					if msg[0] not in CHANNEL_PREFIXES:
						self.steamSay(sourceid, "You are listening to multiple channels (%s) "
							"so I don't know where you want this to go. Prefix messages with target." % ", ".join(channels))
					else:
						channel, msg = msg.split(" ", 1)
						if channel not in channels:
							return self.steamSay(sourceid, "You aren't listening to that channel so I can't send to it.")
						if channel not in self.container.state.channels:
							channels.remove(channel)
							return self.steamSay(sourceid, "I'm not in %s for some reason, "
								"so you can't send to it and won't be receiving messages from it." % channel)
						self.ircSay(channel, msg, user)
	
	def steamStatus(self, sourceid: str, name: str, online: bool) -> None:
		u = self.getUser(sourceid)
		u.name = name
		if not online:
			u.offlinetime = time()
			self.offlineusers.add(u)
		else:
			self.offlineusers.discard(u)
		
	def checkAndStopPoll(self) -> None:
		if self.poller: 
			self.poller.stop()
			self.poller = None
		self.sendready = False
		self.senddict.clear()

	def steamSay(self, userid: str, msg: str) -> None:
		print("SENDING TO (%s): %s" % (userid, repr(msg)))
		self.outbound.setdefault(userid, deque(maxlen=10)).append(msg)
		self.doout = True
	
	def _processOutbound(self) -> None:
		try: userid, msgs = self.outbound.popitem(last=False)
		except KeyError: # special catch in case something weird happens
			self.doout = False
			return
		if not self.outbound:
			self.doout = False
		d = self.senddict.copy()
		print("SENDING BATCH TO (%s) %s" % (userid, self.users[userid].getName()))
		d['steamid_dst'] = userid
		d['text'] = "\n".join(msgs)
		rdata = None
		try: rdata = self.session.post(SENDMSG_URL, d)
		except ConnectionError:
			print("Connection error, retrying send...")
			try: rdata = self.session.post(SENDMSG_URL, d)
			except ConnectionError:
				print("CONNECTION ERROR. DID NOT SEND:", d)
				print_exc()
		if rdata is not None:
			try:
				rdata.raise_for_status()
			except Exception:
				print("ERROR ON OUTBOUND, assume disconnected.")
				print_exc()
				self.oauth = None
				self.checkAndStopPoll()
		
	# login to steamcommunity and get oauth token if not already have.
	# if oauth token gotten, log in to webchat and start poller
	def login(self) -> None:
		# get username and password from moduleoptions
		print("ATTEMPTING LOGIN")
		self.checkAndStopPoll()
		if not self.oauth:
			username = self.container.getOption("username", module="steamchat")
			password = self.container.getOption("password", module="steamchat")
			if username and password: 
				# get RSAkey for hashing password
				d = {"username" : username}
				rdata = self.session.get(RSAKEY_URL % urlencode(d)).json()
				if rdata['success']: 
					# hash password and attempt login proper to steamcommunity
					d['password'] = b64encode(encrypt(password.encode("utf-8"), PublicKey(int(rdata['publickey_mod'], 16), int(rdata['publickey_exp'], 16)))).decode("ascii")
					d['rsatimestamp'] = rdata['timestamp']
					d['oauth_client_id'] = LOGIN_CLIENT_ID
					rdata = self.session.post(LOGIN_URL, d).json()
					if rdata['success'] and 'oauth' in rdata:
						self.oauth = loads(rdata['oauth'])['oauth_token']
					else:
						print("FAILED DATA: \n%s" % repr(rdata))
		else: print("HAD OAUTH, USING")
		# logged in to steam community, now login to webchat...
		if self.oauth:
			try:
				rcdata = self.session.post(CHAT_LOGIN_URL, {"access_token" : self.oauth})
				rcdata.raise_for_status()
				rcdata = rcdata.json()
				print("LOGGED IN TO WEBCHAT")
			except HTTPError as e:
				print(e)
				self.oauth = None
			else:
				self.senddict = {"access_token" : self.oauth, "umqid" : rcdata['umqid'], "type" : "saytext"}
				self.poller = SteamPoller(self.cmdQueue, self.oauth, rcdata['umqid'], rcdata['message'])
				reactor.callFromThread(reactor.callLater, 2.0, self.poller.start)
				self.sendready = True
		#persist oauth key (even if we ended up trashing the old one, it might not be valid anymore)
		if not self.oauth:
			print("FAILED TO LOGIN, DOING COOLDOWN")
			self.cooldownuntil = time() + COOLDOWN
			self.checkAndStopPoll()
		self.container.setOption("oauthtoken", self.oauth, module="steamchat", channel=False)
		#persist oauth token
		blockingCallFromThread(reactor, Settings.saveOptions)
	
	def listUsers(self, dest: str) -> None:
		users = self.channels.get(dest)
		if users:
			if len(users) > 2:
				msg = "Users listening to (%s): %%s" % dest
				title = "Users listening to (%s)" % dest
				items = [USER_ITEM % (u.id, u.getName()) for u in users]
				pastehelper(SteamIRCBotWrapper(Event(None, target=dest), self.container, self), msg, items=items, altmsg="%s", title=title)
			else:
				for u in users:
					self.ircSay(dest, USER_ITEM % (u.id, u.getName()))
		else:
			self.ircSay(dest, "No one listening in here.")

	def leftIRCChannel(self, channel: str) -> None:
		#remove all users from channel
		for u in self.channels.get(channel, []):
			self.removeUserFromChannel(u, channel, sayIRC=False)
		
	def kickUser(self, target: str, user: str) -> None:
		u = self.findUser(user)
		if u:
			if target in u.channels:
				self.removeUserFromChannel(u, target)
			else:
				self.ircSay(target, "(%s) isn't listening in here.")
		else:
			self.ircSay(target, "Don't know (%s)" % user)
	
	def fromIRC(self, func: str, *args: Any) -> None:
		self.cmdQueue.put((func, args))
		
	def stop(self) -> None:
		self.cmdQueue.put(("QUIT", ()))
		#self.join()

CHAT_THREADS: dict[str, SteamChat] = {} #network : SteamChat

def steamchatcmd(event: Event, bot: BotLike) -> None:
	""" steamchat [kick user]. steamchat without arguments will display currently joined/listening steam persons.
	steamchat kick user will kick the supplied user from listening/sending to this channel.
	"""
	if event.argument:
		command, argument = commandSplit(event.argument)
		if command == "kick" and argument:
			cthread = CHAT_THREADS.get(bot.network)
			if cthread: cthread.fromIRC("kickUser", event.nick if event.isPM() else event.target, argument)
			else: bot.say("Error: No Steamchat available for this network.")
		else:
			bot.say(functionHelp(steamchatcmd))
	else:
		#list all
		cthread = CHAT_THREADS.get(bot.network)
		if cthread: cthread.fromIRC("listUsers", event.nick if event.isPM() else event.target)
		else: bot.say("Error: No Steamchat available for this network.")

def doleft(event: Event, bot: BotLike) -> None:
	cthread = CHAT_THREADS.get(bot.network)
	if cthread: cthread.fromIRC("leftIRCChannel", event.target)
	
def relaymsg(event: Event, bot: BotLike) -> None:
	if not event.isPM():
		cthread = CHAT_THREADS.get(bot.network)
		if cthread: cthread.fromIRC("ircMSG", event.target, event.nick, event.msg)

# TODO: This basically uses a minimal version of assembleMsgWLen without the "Len" part and unicode trimming.
#       Don't know if that means we actually need to refactor stuff, or just keep that in mind.
# THINGS PROCESSING SENDMSG MUST NOT RAISE EXCEPTION EVER
def processBotSendmsg(event: Event, bot: BotLike) -> None:
	try:
		if not event.isPM():
			cthread = CHAT_THREADS.get(bot.network)
			if cthread:
				event_msg = event.msg
				if event_msg is None:
					return
				strins = event.kwargs.get("strins")
				if strins:
					joinsep = event.kwargs.get("joinsep")
					if joinsep is not None: msg = event_msg.format(joinsep.join(strins))
					else: msg = event_msg.format(*strins)
				else: msg = event_msg
				steamSource = event.kwargs.get("steamSource")
				cthread.fromIRC("ircMSG", event.target, event.nick, msg, steamSource)
	except Exception:
		print("SENDMSG EVENT EXCEPTION")
		print_exc()

def init(bot: BotLike) -> bool:
	global CHAT_THREADS # oh nooooooooooooooooo
	if bot.getOption("enablestate"):
		if bot.network not in CHAT_THREADS:
			CHAT_THREADS[bot.network] = SteamChat(bot.container, bot.getOption("commandprefix"), 
				bot.getOption("allowedModules", module="steamchat")) # bit silly, but whatever
		else:
			print("WARNING: Already have thread for (%s) network." % bot.network)
	else:
		raise ConfigException('steamchat module requires "enablestate" option')
	return True
	
def unload() -> None:
	for cthread in CHAT_THREADS.values():
		cthread.stop()

mappings = (Mapping(types=["privmsged"], function=relaymsg), Mapping(types=("kickedFrom", "left"), function=doleft),
	Mapping(command=("steamchat", "sc"), function=steamchatcmd), Mapping(["sendmsg"], function=processBotSendmsg),)
