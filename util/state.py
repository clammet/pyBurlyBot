from collections.abc import Iterable
from typing import Any

from .helpers import PrefixMap
#preliminary State

# State MUST be READ ONLY from modules.
# Iteration MUST be performed over copies of .keys() and such else RuntimeError will most likely be raised.
# went this route because I don't want to copy these containers when it isn't really necessary and they may be huge.
# TODO: devise proper way to go about the above with minimal (nested?) copying

from time import time

class Channel:
	def __init__(self, name: str, modes: Any = None) -> None:
		self.name = name
		self.users: dict[str, User] = {} # [nick] = User
		self.ops: set[str] = set() # set of nicks
		self.voices: set[str] = set() # set of nicks
		
		self.moderated = False
		self.inviteonly = False
		self.secret = False
		self.key: str | None = None
		self.private = False
		self.limit: str | None = None
		self.optopic = False
		self.noextmsg = False
		
		# NOTE: this will only be populated with what the bot sees
		# If you want this to be fully populated MODE #channel <b,e,I> will need to be issued
		# Also NOTE: exceptlist is a list of ban exceptions, invitelist is a list of users
		#	exempted from invite only.
		self.banlist: dict[str, tuple[str | None, int]] = {} # [host] = nickwhosetban, time
		self.exceptlist: dict[str, tuple[str | None, int]] = {} # [host] = nickwhosetexcept, time
		self.invitelist: dict[str, tuple[str | None, int]] = {} # [host] = nickwhosetinvite, time
		
		self.topic = ""
		# (nick, ident, host, hostmask)
		self.topicsetby: tuple[str | None, str | None, str | None, str | None] = (None, None, None, None)
		
	def _resetModeIs(self) -> None:
		self.moderated = False
		self.inviteonly = False
		self.secret = False
		self.key = None
		self.private = False
		self.limit = None
		self.optopic = False
		self.noextmsg = False

	def _adduser(self, user: User, modes: Any = None) -> None:
		if user.nick not in self.users:
			self.users[user.nick] = user
		user.channels.add(self.name)
			
	def _changeuser(self, old: str, new: str) -> None:
		self.users[new] = self.users[old]
		del self.users[old]
		
	def _removeuser(self, nick: str) -> None:
		if nick in self.users:
			del self.users[nick]
	
	def _settopic(self, topic: str, nick: str | None, ident: str | None,
		host: str | None, hostmask: str | None) -> None:
		self.topic = topic
		self.topicsetby = (nick, ident, host, hostmask)

class User:
	def __init__(self, nick: str, ident: str | None=None, host: str | None=None,
		hostmask: str | None=None) -> None:
		self.channels: set[str] = set()
		self.nick = nick
		self.ident = ident
		self.host = host
		self.hostmask = host
		
	# TODO: Should we really be caring enough to update hostmaks and stuff
	#	whenever we see the user do something?
	#	Should we actually be tracking the hostname and stuff? or just the nick?
	def _refresh(self, ident: str | None, host: str | None, hostmask: str | None) -> None:
		if ident and self.ident != ident:
			self.ident = ident
		if host and self.host != host:
			self.host = host
		if hostmask and self.hostmask != hostmask:
			self.hostmask = hostmask
				

# TODO: function_renaming_cuz_conventions ~grifftask
class Network:
	def __init__(self, network: str) -> None:
		self.name = network
		self.users: dict[str, User] = {} # [nick] = User
		self.channels: dict[str, Channel] = {}
		self.motd: str | None = None
		self.prefixmap: PrefixMap
	
	def _resetnetwork(self) -> None:
		#clear channels
		self.channels = {}
		self.users = {}
		
	def _nukechannel(self, channel: str) -> None:
		if channel in self.channels:
			for user in self.channels[channel].users.values():
				user.channels.remove(channel)
				if not user.channels:
					#user not known in any channels, remove existance
					del self.users[user.nick]
			del self.channels[channel]

	def _userquit(self, nick: str) -> None:
		if nick in self.users:
			u = self.users[nick]
			for channel in u.channels:
				self.channels[channel]._removeuser(u.nick)
			del self.users[nick]

	def _joinchannel(self, channel: str) -> None:
		self._nukechannel(channel)
		self.channels[channel] = Channel(channel)

	def _leavechannel(self, channel: str) -> None:
		self._nukechannel(channel)

	def _userjoin(self, channel: str, nick: str, ident: str | None=None,
		host: str | None=None, hostmask: str | None=None) -> None:
		if nick not in self.users:
			u = User(nick, ident, host, hostmask)
			self.users[nick] = u
		else:
			u = self.users[nick]
			if ident or host or hostmask: u._refresh(ident, host, hostmask)
		self.channels[channel]._adduser(u)

	@staticmethod
	def _processlist(
		l: Iterable[tuple[str, Any, str | int, str | None]],
	) -> dict[str, tuple[str | None, int]]:
		d = {}
		for (mask, _, t, nick) in l:
			d[mask] = (nick, int(t))
		return d
	
	def _addinvites(self, channel: str,
		invitelist: Iterable[tuple[str, Any, str | int, str | None]]) -> None:
		self.channels[channel].invitelist = self._processlist(invitelist)
	
	def _addexcepts(self, channel: str,
		exceptlist: Iterable[tuple[str, Any, str | int, str | None]]) -> None:
		self.channels[channel].exceptlist = self._processlist(exceptlist)
		
	def _addbans(self, channel: str,
		banlist: Iterable[tuple[str, Any, str | int, str | None]]) -> None:
		self.channels[channel].banlist = self._processlist(banlist)

	def _userrename(self, oldnick: str, newnick: str, ident: str | None,
		host: str | None, hostmask: str | None) -> None:
		user = self.users[oldnick]
		user.nick = newnick
		user._refresh(ident, host, hostmask)
		del self.users[oldnick]
		self.users[newnick] = user
		#go through channels user is on
		for chan in user.channels:
			self.channels[chan]._changeuser(oldnick, newnick)

	def _userpart(self, channel: str, nick: str, ident: str | None=None,
		host: str | None=None, hostmask: str | None=None) -> None:
		if nick in self.users:
			u = self.users[nick]
			self.channels[channel]._removeuser(u.nick)
			if not u.channels:
				#user not known in any channels, remove existance
				del self.users[nick]
			else:
				if ident or host or hostmask: u._refresh(ident, host, hostmask)
				u.channels.remove(channel)
		else:
			# TODO: remove this print, debug
			print("WARNING: user (%s) was never known about... 2SPOOKY" % nick)

	# TODO: This could probably be less wordly, also check if KeyErrors and pop's
	#	will present a problem
	# TODO: also allow tracking of current bot user modes
	def _modechange(self, channel: str, nick: str | None,
		added: Iterable[tuple[str, str]], removed: Iterable[tuple[str, str]],
		reset: bool=True) -> None:
		c = self.channels[channel]
		if reset: c._resetModeIs()
		for mode, arg in added:
			if mode in self.prefixmap.opcmds:
				c.ops.add(arg)
			elif mode == "b":
				c.banlist[arg] = (nick, int(time()))
			elif mode == "e":
				c.exceptlist[arg] = (nick, int(time()))
			elif mode == "i":
				c.inviteonly = True
			elif mode == "m":
				c.moderated = True
			elif mode == "n":
				c.noextmsg = True
			elif mode == "p":
				c.private = True
			elif mode == "s":
				c.secret = True
			elif mode == "t":
				c.optopic = True
			elif mode == "k":
				c.key = arg
			elif mode == "l":
				c.limit = arg
			elif mode == "I":
				c.invitelist[arg] = (nick, int(time()))
			elif mode in self.prefixmap.voicecmds:
				c.voices.add(arg)
				
		for mode, arg in removed:
			if mode in self.prefixmap.opcmds:
				try: c.ops.remove(arg)
				except KeyError: pass
			elif mode == "b":
				c.banlist.pop(arg, None)
			elif mode == "e":
				c.exceptlist.pop(arg, None)
			elif mode == "i":
				c.inviteonly = False
			elif mode == "m":
				c.moderated = False
			elif mode == "n":
				c.noextmsg = False
			elif mode == "p":
				c.private = False
			elif mode == "s":
				c.secret = False
			elif mode == "t":
				c.optopic = False
			elif mode == "k":
				c.key = None
			elif mode == "l":
				c.limit = None
			elif mode == "I":
				c.invitelist.pop(arg, None)
			elif mode in self.prefixmap.voicecmds:
				try: c.voices.remove(arg)
				except KeyError: pass

	def _settopic(self, channel: str, newtopic: str, nick: str | None=None,
		ident: str | None=None, host: str | None=None, hostmask: str | None=None) -> None:
		self.channels[channel]._settopic(newtopic, nick, ident, host, hostmask=None)
		
	def _addusers(self, channel: str, users: Iterable[str]) -> None:
		for nick in users:
			prefix = nick[0]
			if prefix in self.prefixmap.nickprefixes:
				nick = nick.lstrip(self.prefixmap.nickprefixes)
				
			self._userjoin(channel, nick)
			if prefix in self.prefixmap.opprefixes:
				self.channels[channel].ops.add(nick)
			if prefix in self.prefixmap.voiceprefixes:
				self.channels[channel].voices.add(nick)
