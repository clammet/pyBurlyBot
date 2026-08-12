from re import Match
from typing import Any
from twisted.words.protocols.irc import CHANNEL_PREFIXES
from .helpers import coerceToUnicode

from time import time
from datetime import datetime

# NOTHING IN EVENT SHOULD BE MODIFIED BY MODULES EVER, THANKS.
# TODO: I think prefix and hostmask are always the same. What to do?
class Event:
	regex_match: Match[str]

	def __init__(self, type: str | None, prefix: str | None=None, params: list[str] | None=None,
		hostmask: str | None=None, target: str | None=None, msg: str | None=None,
		nick: str | bytes | None=None, ident: str | bytes | None=None,
		host: str | None=None, encoding: str="utf-8", command: str | None=None,
		argument: str | None=None, priority: int=10, **kwargs: Any) -> None:
		self.type = type
		self.prefix = prefix
		self.params = params
		self.hostmask = hostmask
		self.nick: str | None = coerceToUnicode(nick, encoding) if nick else None
		self.ident: str | None = coerceToUnicode(ident, encoding) if ident else None
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
	
	def __repr__(self) -> str:
		return "Event(type=%r, prefix=%r, params=%r, hostmask=%r, nick=%r, ident=%r, host=%r, "\
			"target=%r, msg=%r, command=%r, argument=%r, kwargs=%r, time=%r" % \
				(self.type, self.prefix, self.params, self.hostmask, self.nick, self.ident, self.host, 
				self.target, self.msg, self.command, self.argument, self.kwargs, self.time)
	def __str__(self) -> str: return self.__repr__()
		
	def __getattr__(self, name: str) -> Any:
		# return attr if it exists, else return the one in kwargs
		try: return self.__dict__[name]
		except KeyError:
			return getattr(self, "kwargs")[name] # will raise KeyError if requested kwarg doesn't exist
	
	
	# TODO: Should this be called "isQuery" ?
	def isPM(self) -> bool:
		return self.target is not None and self.target[0] not in CHANNEL_PREFIXES
