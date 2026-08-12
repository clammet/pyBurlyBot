from twisted.words.protocols.irc import CHANNEL_PREFIXES
from .helpers import coerceToUnicode

from time import time
from datetime import datetime

# NOTHING IN EVENT SHOULD BE MODIFIED BY MODULES EVER, THANKS.
# TODO: I think prefix and hostmask are always the same. What to do?
class Event:
	def __init__(self, type, prefix=None, params=None, hostmask=None, target=None, msg=None, 
		nick=None, ident=None, host=None, encoding="utf-8", command=None, argument=None, priority=10, **kwargs):
		self.type = type
		self.prefix = prefix
		self.params = params
		self.hostmask = hostmask
		self.nick = coerceToUnicode(nick, encoding) if nick else nick
		self.ident = coerceToUnicode(ident, encoding) if ident else ident
		# Note: if unicode/punycode hostnames becomes a thing for IRC, .decode("idna") I guess
		self.host = host
		
		self.target = coerceToUnicode(target, encoding) if target else target
		
		# if there is a msg, it's already unicode (done in dispatcher.)
		self.msg = msg
		
		self.command = command
		self.argument = argument
		
		# kwargs is a dict of uncommon event attributes which will be looked up on attribute access
		self.kwargs = kwargs
		
		# might be useful
		self.time = time()
		self.dtime = datetime.now()
		self.priority = priority
	
	def __repr__(self):
		return "Event(type=%r, prefix=%r, params=%r, hostmask=%r, nick=%r, ident=%r, host=%r, "\
			"target=%r, msg=%r, command=%r, argument=%r, kwargs=%r, time=%r" % \
				(self.type, self.prefix, self.params, self.hostmask, self.nick, self.ident, self.host, 
				self.target, self.msg, self.command, self.argument, self.kwargs, self.time)
	def __str__(self): return self.__repr__()
		
	def __getattr__(self, name):
		# return attr if it exists, else return the one in kwargs
		try: return self.__dict__[name]
		except KeyError:
			return getattr(self, "kwargs")[name] # will raise KeyError if requested kwarg doesn't exist
	
	
	# TODO: Should this be called "isQuery" ?
	def isPM(self):
		return self.target[0] not in CHANNEL_PREFIXES
