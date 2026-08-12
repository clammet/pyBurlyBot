#pyBurlyBot

# twisted imports
from twisted.words.protocols.irc import IRCClient, IRCBadModes, parseModes, X_DELIM, \
	symbolic_to_numeric, numeric_to_symbolic, ctcpExtract, lowQuote
from twisted.internet import reactor
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.python import log
from twisted.protocols.basic import LineReceiver
from twisted.protocols.policies import TimeoutMixin
from OpenSSL import SSL

# system imports
from time import time
from collections import deque
from math import floor

#BurlyBot imports
from .helpers import processHostmask, processListReply, PrefixMap, isIterable, splitEncodedUnicode

def _tlsConnectionErrorHint(settings, reason):
	if not getattr(settings, "ssl", False) or not reason.check(SSL.Error):
		return None
	return (
		"[TLS connection hint: check that the configured port accepts direct TLS; "
		f"attempted {settings.host}:{settings.port}.]"
	)

# inject some other common symbolic IDs:
symbolic_to_numeric["RPL_YOURID"] = '042'
symbolic_to_numeric["RPL_LOCALUSERS"] = '265'
symbolic_to_numeric["RPL_GLOBALUSERS"] = '266'
symbolic_to_numeric["RPL_CREATIONTIME"] = '329'
symbolic_to_numeric["RPL_HOSTHIDDEN"] = '396'
symbolic_to_numeric["ERR_NOTEXTTOSEND"] = '412'
# and the reverse:
numeric_to_symbolic["042"] = 'RPL_YOURID'
numeric_to_symbolic["265"] = 'RPL_LOCALUSERS'
numeric_to_symbolic["266"] = 'RPL_GLOBALUSERS'
numeric_to_symbolic["329"] = 'RPL_CREATIONTIME'
numeric_to_symbolic["396"] = 'RPL_HOSTHIDDEN'
numeric_to_symbolic["412"] = 'ERR_NOTEXTTOSEND'

class BurlyBot(IRCClient, TimeoutMixin):
	"""BurlyBot"""
	timeOut = 150 # 2.5mins
	
	erroneousNickFallback = "BurlyBot"
	linethrottle = 3
	_lines = 0
	_lastmsg = 0
	_lastCL = None
	supported = None
	altindex = 0
	prefixlen = None
	delimiter = b'\n' # LineReceiver adds this after _reallySendLine appends CR.
	versionName = "pyBurlyBot git"
	realname = "Burly Bot"
	
	# http://twistedmatrix.com/trac/browser/trunk/twisted/words/protocols/irc.py
	# irc_ and RPL_ methods are duplicated here verbatim so that we can dispatch higher level
	# events with the low level data intact.
	
	# custom sendline throttler. This might be overly complex but should behave similar to mIRC
	# where lines are only throttled once you cross a threshold. I don't know if the cooldown is similar though
	def sendLine(self, line):
		if isinstance(line, bytes):
			line = line.decode(self.settings.encoding, "replace")
		encoded = line.encode(self.settings.encoding)
		if len(encoded) > 510:
			line = encoded[:510].decode(self.settings.encoding, "ignore")
		t = time()
		if self._lastmsg + 1 < t:
			# if message hasn't been sent for 1 seconds, go for it
			self._lines += 1
			self._reallySendLine(line)
			# also reset the linecount if no msg for 2 seconds
			if self._lastmsg + 2 < t:
				self._lines = 1
		elif self._lines < self.linethrottle:
			# under threshold, go for it
			self._lines += 1
			self._reallySendLine(line)
		else:
			# cross threshold in 1 second, slow down
			self._dqueue.append(line)
			if not self._lastCL:
				self._lastCL = reactor.callLater(1.0, self._sendLine)
		self._lastmsg = t
		
	def _sendLine(self):
		t = time()
		if self._dqueue:
			line = self._dqueue.popleft()
			self._reallySendLine(line)
			self._lastmsg = t
			if self._dqueue:
				self._lastCL = reactor.callLater(1.0, self._sendLine)
			else:
				self._lastCL = None
		else:
			self._lastCL = None

	# sticking to specification
	def _reallySendLine(self, line):
		quoted_line = lowQuote(line).encode(self.settings.encoding) + b'\r'
		if self.debug >= 2: print("REALLY SENDING LINE:", repr(quoted_line + self.delimiter))
		return LineReceiver.sendLine(self, quoted_line)
	def dataReceived(self, data):
		self.resetTimeout()
		IRCClient.dataReceived(self, data)
	
	def names(self, channels):
		"""
		List the users in a channel.
		"""
		if isIterable(channels):
			self.sendLine('NAMES %s' % ",".join(channels))
		else:
			self.sendLine('NAMES %s' % channels)

	def banlist(self, channel):
		self.mode(channel, True, "b")
	
	###
	### The following are "low level events" almost (probably, maybe butchered) verbatim from IRCClient
	###
	def irc_JOIN(self, prefix, params):
		"""
		Called when a user joins a channel.
		"""
		nick, ident, host = processHostmask(prefix)
		channel = params[-1]
		if nick == self.nickname:
			# take note of our prefix! (for message length calculation
			self.prefixlen = len(prefix)
			if self.state: 
				self.state._joinchannel(channel)
				self.sendLine("MODE %s" % channel)
			self.dispatch(self, "joined", prefix=prefix, params=params, hostmask=prefix, target=channel, 
				nick=nick, ident=ident, host=host)
		else:
			if self.state: self.state._userjoin(channel, nick, ident, host, prefix)
			self.dispatch(self, "userJoined", prefix=prefix, params=params, hostmask=prefix, target=channel, 
				nick=nick, ident=ident, host=host)

	def irc_PART(self, prefix, params):
		"""
		Called when a user leaves a channel.
		"""
		nick, ident, host = processHostmask(prefix)
		channel = params[0]
		if nick == self.nickname:
			# take note of our prefix! (for message length calculation
			self.prefixlen = len(prefix)
			if self.state: self.state._leavechannel(channel)
			self.dispatch(self, "left", prefix=prefix, params=params, hostmask=prefix, target=channel, 
				nick=nick, ident=ident, host=host)
		else:
			if self.state: self.state._userpart(channel, nick, ident, host, prefix)
			self.dispatch(self, "userLeft", prefix=prefix, params=params, hostmask=prefix, target=channel, 
				nick=nick, ident=ident, host=host)
			

	def irc_QUIT(self, prefix, params):
		"""
		Called when a user has quit.
		"""
		nick, ident, host = processHostmask(prefix)
		if self.state: self.state._userquit(nick)
		self.dispatch(self, "userQuit", prefix=prefix, params=params, hostmask=prefix, msg=params[0], 
			nick=nick, ident=ident, host=host)

	def irc_MODE(self, prefix, params):
		"""
		Parse a server mode change message.
		"""
		channel, modes, args = params[0], params[1], params[2:]

		if modes[0] not in '-+':
			modes = '+' + modes

		if channel == self.nickname:
			# This is a mode change to our individual user, not a channel mode
			# that involves us.
			paramModes = self.getUserModeParams()
			# take note of our prefix! (for message length calculation
			self.prefixlen = len(prefix)
		else:
			paramModes = self.getChannelModeParams()

		try:
			added, removed = parseModes(modes, args, paramModes)
		except IRCBadModes:
			log.err(None, 'An error occured while parsing the following '
						  'MODE message: MODE %s' % (' '.join(params),))
		else:
			nick, ident, host = processHostmask(prefix)
			if self.state and (channel != self.nickname):
				self.state._modechange(channel, nick, added, removed)
			self.dispatch(self, "modeChanged", prefix=prefix, params=params, hostmask=prefix, target=channel,
				added=added, removed=removed, modes=modes, args=args, nick=nick, ident=ident, host=host)

	def irc_PRIVMSG(self, prefix, params):
		"""
		Called when we get a message.
		"""
		if self.debug >= 2: print("INCOMING PRIVMSG:", prefix, params)
		user = prefix
		channel = params[0]
		message = params[-1]
		if not message:
			# Don't raise an exception if we get blank message.
			return

		if message[0] == X_DELIM:
			m = ctcpExtract(message)
			if m['extended']:
				self.ctcpQuery(user, channel, m['extended'], params)

			if not m['normal']:
				return

			message = ' '.join(m['normal'])
		
		nick, ident, host = processHostmask(prefix)
		if nick == self.nickname:
			# take note of our prefix! (for message length calculation
			self.prefixlen = len(prefix)
		# These are actually messages, ctcp's aren't dispatched here
		self.dispatch(self, "privmsged", prefix=prefix, params=params, hostmask=user, target=channel, msg=message, 
			nick=nick, ident=ident, host=host)

	def irc_NOTICE(self, prefix, params):
		"""
		Called when a user gets a notice.
		"""
		if self.debug >= 2: print("INCOMING NOTICE:", prefix, params)
		user = prefix
		channel = params[0]
		message = params[-1]

		if message[0] == X_DELIM:
			m = ctcpExtract(message)
			if m['extended']:
				self.ctcpReply(user, channel, m['extended'], params)
			if not m['normal']:
				return
			message = ' '.join(m['normal'])
		
		nick, ident, host = processHostmask(prefix)
		if nick == self.nickname:
			# take note of our prefix! (for message length calculation
			self.prefixlen = len(prefix)
		self.dispatch(self, "noticed", prefix=prefix, params=params, hostmask=user, target=channel, msg=message,
			nick=nick, ident=ident, host=host)

	def irc_NICK(self, prefix, params):
		"""
		Called when a user changes their nickname.
		"""
		nick, ident, host = processHostmask(prefix)
		
		if nick == self.nickname:
			# take note of our prefix! (for message length calculation
			self.prefixlen = len(prefix)
			self.nickChanged(params[0])
			self.dispatch(self, "nickChanged", prefix=prefix, params=params, hostmask=prefix, newname=params[0])
		else:
			if self.state: self.state._userrename(nick, params[0], ident, host, prefix)
			self.dispatch(self, "userRenamed", prefix=prefix, params=params, hostmask=prefix, newname=params[0], 
				nick=nick, ident=ident, host=host)

	def irc_KICK(self, prefix, params):
		"""
		Called when a user is kicked from a channel.
		"""
		kicker = prefix.split('!')[0]
		channel = params[0]
		kicked = params[1]
		message = params[-1]
		if kicked.lower() == self.nickname.lower():
			if self.state: self.state._leavechannel(channel)
			self.dispatch(self, "kickedFrom", prefix=prefix, params=params, hostmask=prefix, nick=kicker, target=channel, msg=message, kicked=kicked)
		else:
			if self.state: self.state._userpart(channel, kicked)
			self.dispatch(self, "userKicked", prefix=prefix, params=params, hostmask=prefix, nick=kicker, target=channel, msg=message, kicked=kicked)

	def irc_RPL_TOPIC(self, prefix, params):
		"""
		Called when the topic for a channel is initially reported or when it
		subsequently changes.
		"""
		nick, ident, host = processHostmask(prefix)
		channel = params[1]
		newtopic = params[2]
		
		if self.state: self.state._settopic(channel, newtopic, nick, ident, host)
		self.dispatch(self, "topicUpdated", prefix=prefix, params=params, hostmask=prefix, target=channel, newtopic=newtopic, 
				nick=nick, ident=ident, host=host)

	def irc_RPL_NOTOPIC(self, prefix, params):
		"""
		...
		"""
		nick, ident, host = processHostmask(prefix)
		channel = params[1]
		newtopic = ""
		if self.state: self.state._settopic(channel, newtopic, nick, ident, host)
		self.dispatch(self, "topicUpdated", prefix=prefix, params=params, hostmask=prefix, target=channel, newtopic=newtopic, 
				nick=nick, ident=ident, host=host)

	def irc_RPL_ENDOFMOTD(self, prefix, params):
		"""
		Called when the bot receives RPL_ENDOFMOTD from the server.
		
		motd is a list containing the accumulated contents of the message of the day.
		"""
		motd = self.motd
		if self.state: self.state._motd = motd
		# The following sets self.motd to None, so we get the motd first
		IRCClient.irc_RPL_ENDOFMOTD(self, prefix, params)
		self.dispatch(self, "receivedMOTD", prefix=prefix, params=params, motd=motd)
			
	def irc_RPL_MYINFO(self, prefix, params):
		info = params[1].split(None, 3)
		while len(info) < 4:
			info.append(None)
		servername, version, umodes, cmodes = info
		self.dispatch(self, "myInfo", prefix=prefix, params=params, servername=servername, version=version, 
			umodes=umodes, cmodes=cmodes)
	
	### The following are custom, not taken from IRCClient:
	def irc_RPL_CHANNELMODEIS(self, prefix, params):
		"""
		Parse a RPL_CHANNELMODEIS message.
		"""
		channel, modes, args = params[1], params[2], params[3:]

		if modes[0] not in '-+':
			modes = '+' + modes
		try:
			added, _ = parseModes(modes, args, self.getChannelModeParams())
		except IRCBadModes:
			log.err(None, 'An error occured while parsing the following '
						  'MODE message: MODE %s' % (' '.join(params),))
		else:
			if self.state:
				self.state._modechange(channel, None, added, [])
			self.dispatch(self, "channelModeIs", prefix=prefix, params=params, hostmask=prefix, target=channel,
				added=added, modes=modes, args=args)

	def irc_RPL_CREATIONTIME(self, prefix, params):
		channel = params[1]
		t = params[2]
		self.dispatch(self, "creationTime", prefix=prefix, params=params, target=channel, creationtime=t)

	def irc_RPL_NAMREPLY(self, prefix, params):
		"""
		Called when NAMES reply is received from the server.
		"""
		channel = params[2]
		users = params[3].split()
		# TODO: should we give this event a copy of PrefixMap? check state._addusers as for why
		if self.state: self.state._addusers(channel, users)
		self._names.setdefault(channel, []).extend(users)
		self.dispatch(self, "nameReply", prefix=prefix, params=params, target=channel, users=users)


	def irc_RPL_ENDOFNAMES(self, prefix, params):
		channel = params[1]
		self.dispatch(self, "endOfNames", prefix=prefix, params=params, target=channel, users=self._names.pop(channel, []))

	def irc_RPL_BANLIST(self, prefix, params):
		"""
		Called when RPL_BANLIST reply is received from the server.
		"""
		channel, banmask, nick, ident, host, t, hostmask = processListReply(params)
		
		self._banlist.setdefault(channel, []).append((banmask, hostmask, t, nick))
		self.dispatch(self, "banList", prefix=prefix, params=params, target=channel, banmask=banmask, hostmask=hostmask, 
			timeofban=t, nick=nick, ident=ident, host=host)

	def irc_RPL_ENDOFBANLIST(self, prefix, params):
		channel = params[1]
		banlist = self._banlist.pop(channel, [])
		if self.state: self.state._addbans(channel, banlist)
		self.dispatch(self, "endOfBanList", prefix=prefix, params=params, target=channel, banlist=banlist)
			
	def irc_RPL_EXCEPTLIST(self, prefix, params):
		"""
		Called when RPL_EXCEPTLIST reply is received from the server.
		"""
		channel, exceptmask, nick, ident, host, t, hostmask = processListReply(params)

		self._exceptlist.setdefault(channel, []).append((exceptmask, hostmask, t, nick))
		self.dispatch(self, "exceptList", prefix=prefix, params=params, target=channel, exceptmask=exceptmask, hostmask=hostmask, 
			timeofban=t, nick=nick, ident=ident, host=host)

	def irc_RPL_ENDOFEXCEPTLIST(self, prefix, params):
		channel = params[1]

		exceptlist = self._exceptlist.pop(channel, [])
		if self.state: self.state._addexcepts(channel, exceptlist)
		self.dispatch(self, "endOfBanList", prefix=prefix, params=params, target=channel, exceptlist=exceptlist)

	def irc_RPL_INVITELIST(self, prefix, params):
		"""
		Called when RPL_INVITELIST reply is received from the server.
		"""
		channel, invitemask, nick, ident, host, t, hostmask = processListReply(params)

		self._invitelist.setdefault(channel, []).append((invitemask, hostmask, t, nick))
		self.dispatch(self, "inviteList", prefix=prefix, params=params, target=channel, invitemask=invitemask, hostmask=hostmask, 
			timeofban=t, nick=nick, ident=ident, host=host)

	def irc_RPL_ENDOFINVITELIST(self, prefix, params):
		channel = params[1]

		invitelist = self._invitelist.pop(channel, [])
		if self.state: self.state._addinvites(channel, invitelist)
		self.dispatch(self, "endOfInviteList", prefix=prefix, params=params, target=channel, invitelist=invitelist)

	def irc_RPL_ISUPPORT(self, prefix, params):
		IRCClient.irc_RPL_ISUPPORT(self, prefix, params)
		# This seems excessive but it's the only way to reliably update the prefixmap
		self.prefixmap.loadfromprefix(iter(self.supported.getFeature("PREFIX").items()))
		
	# This method is interesting, for example ERROR gets sent from Rizon when you quit
	# TODO: find out what to actually do with this.
	def irc_ERROR(self, prefix, params):
		print("ERROR received: %s" % params)
		
	###
	### Modified command handler from IRCCLient
	###
	def handleCommand(self, command, prefix, params):
		"""
		Determine the function to call for the given command and call it with
		the given arguments.
		"""
		method_name = "irc_%s" % command.upper()
		method = getattr(self, method_name, None)
		try:
			if callable(method):
				method(prefix, params)
		except Exception:
			log.deferr()
		else:
			# All low level (RPL_type) events dispatched as they are
			# These will either be numeric or symbolic, so we also dispatch the
			# corresponding symbolic/numeric event when possible for ease of use
			self.dispatch(self, command, prefix=prefix, params=params)
			if command.upper() in symbolic_to_numeric:
				self.dispatch(self, symbolic_to_numeric[command.upper()], prefix=prefix, params=params)
			elif command in numeric_to_symbolic:
				self.dispatch(self, numeric_to_symbolic[command], prefix=prefix, params=params)
			if method is None:
				self.irc_unknown(prefix, command, params)

	def lineReceived(self, line):
		if isinstance(line, bytes):
			line = line.decode(self.settings.encoding, "replace")
		if self.debug >= 3: print("INCOMING LINE: %s" % line)
		IRCClient.lineReceived(self, line)
				
	###
	### The following are "preprocessed" events normally called from IRCClient and mostly duplicated from IRCClient
	###
	def ctcpQuery(self, user, channel, messages, params):
		"""
		Dispatch method for any CTCP queries received.
		Duplicate tags ignored.
		Override from IRCClient
		"""
		seen = set()
		nick, ident, host = processHostmask(user)
		if nick == self.nickname:
			# take note of our prefix! (for message length calculation
			self.prefixlen = len(user)
		for tag, data in messages:
			if tag not in seen:
				#dispatch event					
				self.dispatch(self, "ctcpQuery", prefix=user, params=params, hostmask=user, target=channel, tag=tag, 
					data=data, nick=nick, ident=ident, host=host)
				#call handler if defined:
				method = getattr(self, 'ctcpQueryB_%s' % tag, None)
				if method is not None: method(user, channel, data)
				else: self.ctcpUnknownQuery(user, channel, tag, data)
			seen.add(tag)
	
	# borrowed mostly from IRCClient
	def ctcpQueryB_VERSION(self, user, channel, data):
		if self.versionName:
			nick = user.split('!')[0]
			veritems = [self.versionName]
			if self.versionNum: veritems.append(self.versionNum)
			if self.versionEnv: veritems.append(self.versionEnv)
			self.ctcpMakeReply(nick, [('VERSION', ';'.join(veritems))])
	
	def ctcpReply(self, user, channel, messages, params):
		"""
		Dispatch method for any CTCP replies received.
		Duplicate tags ignored.
		Override from IRCClient
		"""
		seen = set()
		nick, ident, host = processHostmask(user)
		###
		# Commented because the prefix variable here will throw a NameError
		# and not sure how to fix
		###
		# if nick == self.nickname:
			# take note of our prefix! (for message length calculation
			# self.prefixlen = len(prefix)
		for tag, data in messages:
			if tag not in seen:
				#dispatch event
				self.dispatch(self, "ctcpReply", prefix=user, params=params, hostmask=user, target=channel, tag=tag, 
					data=data, nick=nick, ident=ident, host=host)
			seen.add(tag)

	def ctcpUnknownQuery(self, user, channel, tag, data):
		if self.settings.debug:
			print('Unknown CTCP query from %r: %r %r' % (user, tag, data))

	def signedOn(self):
		"""
		Called when bot has successfully signed on to server.
		"""
		print("[Signed on]")
		
		#process nickprefixes
		# reason for this is to class prefixes in to "op" and "voice"
		# and reason for that is because most important IRC operations are classed on OP or VOICE
		self.prefixmap = PrefixMap(iter(self.supported.getFeature("PREFIX").items()))
		if self.state:
			self.state.prefixmap = self.prefixmap
		
		self.container._setBotinst(self)
		if self.state: self.state._resetnetwork()
		
		# allow modules to implement a delay or somesuch for joining channels if they handle this event
		if not self.dispatch(self, "preJoin"):
			for chan in self.settings.channels:
				self.join(*chan)
		self.dispatch(self, "signedOn")

	# TODO: this currently doesn't get called. Do we want to dispatch these events? Or just make
	# module catch CTCP events and check for ACTION tag?
	def action(self, hostmask, channel, msg, params):
		"""
		This will get called when the bot sees someone do an action.
		"""
		pass
	
	#overriding msg
	def msg(self, user, msg, length=None, strins=None):
		raise NotImplementedError("Use sendmsg instead.")
		
	# override the method that determines how a nickname is changed on
	# collisions.
	# TODO: At the moment this attempts to iterate the altnicks if it exists and falls back to
	# suffix after iterating. When to reset the iteration? At the moment it does it on connection
	# should probably make a reactor.callLater, and cancel it on disconnect or something.
	def alterCollidedNick(self, nickname):
		if self.settings.altnicks:
			if self.altindex < len(self.settings.altnicks):
				s = self.settings.altnicks[self.altindex]
				self.altindex += 1
				return s
			elif nickname != self.settings.nick:
				return self.settings.nick
		return (nickname + self.settings.nicksuffix)
		
	def irc_unknown(self, prefix, command, params):
		if self.settings.debug:
			print("Unknown command: %s, %s, %s" % (prefix, command, params))
	
	###
	### Custom outgoing methods
	###
	# TODO: Need to add more of these for hooking other outbound events maybe, like notice...
	def sendmsg(self, target, msg, direct=False, split=False, **kwargs):
		#check if there's hooks, if there is, dispatch, if not, send directly
		if self.dispatcher.MSGHOOKS and not direct:
			self.dispatch(self, "sendmsg", target=target, msg=msg, **kwargs)
		else:
			self.dispatch(self, "sendmsg", target=target, nick=self.nickname, msg=msg, split=split, **kwargs)
			if split:
				for m in self._buildmsg(target, msg, split, **kwargs):
					self.sendLine(m)
			else:
				self.sendLine(self._buildmsg(target, msg, split, **kwargs))
	
	# will return true if sendmsg can proceed without truncation, false otherwise.
	# will provide incorrect results if any sendmsg hooks change lengths of messages
	# TODO: (very low priority I guess) somehow get a builtmsg from sendmsg hooks
	# NOTE: USAGE OF THIS MESSAGE MUST TEST FOR TRUE AND FALSE EXPLICITLY. None will be returned if bot isn't connected
	#		at the time of call.
	def checkSendMsg(self, target, msg):
		return len(self._buildmsg(target, msg, check=True).encode(self.settings.encoding)) <= self.calcAvailableMsgLength("")
		
	def _buildmsg(self, target, message, split=False, check=False, strins=None, **kwargs):
		if not isinstance(message, str): message = str(message)
		if strins:
			if split:
				return (self.assembleMsgWLen('PRIVMSG %s :%s' % (target, msg), strins=strins, **kwargs) for msg in message.split("\n"))
			else:
				return self.assembleMsgWLen('PRIVMSG %s :%s' % (target, message), strins=strins, **kwargs)
		else:
			fmt = 'PRIVMSG %s :%%s' % (target,)
			if split:
				msgs = []
				for msg in message.split("\n"):
					for m in splitEncodedUnicode(message, self.calcAvailableMsgLength(fmt % ""), encoding=self.settings.encoding, n=4):
						msgs.append(fmt % m[0])
				return msgs[:4]
			else:
				if check:
					# blindly truncate message useful for checkSendMsg
					return fmt % message
				else:
					# auto trim message so we don't look bad when sending non interpolated message (check=false)
					return fmt % splitEncodedUnicode(message, self.calcAvailableMsgLength(fmt % ""), encoding=self.settings.encoding)[0][0]
	
	# helper method to automatically truncate string to be replaced
	# TODO: need decide on string format method, either "%s" % x or "{0}".format(x)
	#	For now we are using {0} to make sure no bads with URLencoded URLs
	# TODO: this must accept either string or LIST for strins so that strins can be modified (when doing fcfs.)
	# NOTE: Calculation will be off if NL/CR or any of the "lowQuote" characters are in s or strins.
	# 		You should make sure your data doesn't contain any of those characters (NL/CR/020/NUL)
	def assembleMsgWLen(self, s, strins=None, fcfs=False, joinsep=None):
		enc = self.settings.encoding
		if isinstance(strins, str):
			sl = self.calcAvailableMsgLength(s.format(""))
			if sl <= 0: # case where template string is already too big
				return splitEncodedUnicode(s, len(s)+sl, encoding=enc)[0][0]
			return s.format(splitEncodedUnicode(strins, sl, encoding=enc)[0][0])
		
		ls = len(strins)
		if joinsep is not None: 
			# lj is len(joinsep) when comparing to avail in fcfs add 2 to allow some
			# room for start of next element at least
			if isinstance(joinsep, str): lj = len(joinsep.encode(enc))
			else: lj = len(joinsep)
		if isIterable(strins):
			if joinsep is not None: avail = self.calcAvailableMsgLength(s.format("")) # must be only one replacement
			else: avail = self.calcAvailableMsgLength(s.format(*[""]*ls)) # format with empty strins to calc max avail
			if avail < 0: # case where template string is already too big
				s = s.format(*[""]*ls)
				return splitEncodedUnicode(s, len(s)+avail, encoding=enc)[0][0]
			if fcfs:
				# first come first served
				if not isinstance(strins, list): 
					raise ValueError("Require list/tuple, dict, or string for strins.")
				for i, rep in enumerate(strins):
					# get trimmed replacement and the length of that trimmed replacement
					rep, lrep = splitEncodedUnicode(rep, avail, encoding=enc)[0]
					# track remaining message space left
					avail -= lrep
					#append joinsep if there's room, else make avail 0
					if (joinsep is not None) and (i != ls-1): 
						if avail < lj+2: 
							avail = 0
						else: 
							rep = rep+joinsep
							avail -= lj
					# replace the replacement with the trimmed version
					strins[i] = rep
				if joinsep is not None: return s.format("".join(strins))
				else: return s.format(*strins)
			else:
				# round 2, even divide
				if joinsep is not None: segmentlength = int(floor(avail / ls)) - (ls-1*lj)
				else: segmentlength = int(floor(avail / ls))
				if isinstance(strins, tuple):
					strins = list(strins)
				for i, sr in enumerate(strins):
					if (joinsep is not None) and (i != ls-1):
						strins[i] = splitEncodedUnicode(sr, segmentlength, encoding=enc)[0][0]+joinsep
					else:
						strins[i] = splitEncodedUnicode(sr, segmentlength, encoding=enc)[0][0]
				if joinsep is not None: return s.format("".join(strins))
				else: return s.format(*strins)
			
		elif isinstance(strins, dict):
			# total space available for message
			avail = self.calcAvailableMsgLength(s.format(**dict(((key, "") for key in list(strins.keys()))))) # format with empty strins to calc max avail
			if avail < 0: # case where template string is already too big
				s = s.format(**dict(((key, "") for key in list(strins.keys()))))
				return splitEncodedUnicode(s, len(s)+avail, encoding=enc)[0][0]
			if fcfs:
				# first come first served (NOTE: This doesn't make much sense for an unordered thing like a dictionary)
				# hopefully we are passed an ordered dictionary or something that extends from dict.
				for key, rep in strins.items():
					rep, lrep = splitEncodedUnicode(rep, avail, encoding=enc)[0]
					strins[key] = rep
					avail -= lrep
				return s.format(**strins)
			else:
				# round 2, even divide
				segmentlength = int(floor((avail / ls)))
				for key, value in strins.items():
					strins[key] = splitEncodedUnicode(value, segmentlength, encoding=enc)[0][0]
				return s.format(**strins)
		else:
			raise ValueError("Require list/tuple, dict, or string for strins.")
	
	def calcAvailableMsgLength(self, command):
		if self.prefixlen:
			# 510 = line terminator 508 = something else I'm not knowing about
			return 508 - self.prefixlen - len(lowQuote(command).encode(self.settings.encoding))
		else:
			return self._safeMaximumLineLength(lowQuote(command)) - 2 #line terminator

	###
	### Connection management methods
	###
	def connectionMade(self):
		IRCClient.connectionMade(self)
		self._names = {}
		self._banlist = {}
		self._exceptlist = {}
		self._invitelist = {}
		# TODO: I think this should be on "signedOn()" just in case part of the signon is causing instant disconnect
		# reset connection factory delay:
		self.factory.resetDelay()

	def connectionLost(self, reason):
		IRCClient.connectionLost(self, reason)
		self.container._setBotinst(None)
		if self.state: self.state._resetnetwork()
		# TODO: reason needs to be properly formatted/actual reason being extracted from the "Failure" or whatever
		print("[disconnected: %s]" % reason)
		tls_hint = _tlsConnectionErrorHint(self.settings, reason)
		if tls_hint:
			print(tls_hint)

class BurlyBotFactory(ReconnectingClientFactory):
	"""
	A factory for BurlyBot.
	A new protocol instance will be created each time we connect to the server.
	"""

	# the class of the protocol to build when new connection is made
	protocol = BurlyBot

	def __init__(self, serversettings):
		#reconnect settings
		self.container = serversettings.container
		self.maxDelay = 45
		self.factor = 1.9021605823
	
	def buildProtocol(self, address):
		proto = ReconnectingClientFactory.buildProtocol(self, address)
		proto.container = self.container
		# for shortcut access:
		proto.settings = self.container._settings
		if proto.settings.enablestate: proto.state = self.container.state
		else: proto.state = None
		proto.dispatch = proto.settings.dispatcher.dispatch
		proto.dispatcher = proto.settings.dispatcher
		proto.nickname = proto.settings.nick
		#throttle queue
		proto._dqueue = deque()
		#debug
		proto.debug = proto.settings.debug
		return proto
