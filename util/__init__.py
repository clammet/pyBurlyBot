from re import compile as recompile, IGNORECASE, VERBOSE
from .timer import Timers, TimerExists, TimerInvalidName, TimerNotFound
from .container import TimeoutException
from .helpers import distance_of_time_in_words, processHostmask, commandSplit, argumentSplit, \
	commandlength, functionHelp, coerceToUnicode, parseDateTime, match_hostmask, WDAY_MAP, WDAY_SHORTMAP
from .mapping import Mapping
from .db import fetchone, fetchall, fetchmany

__all__ = (
	"Timers", "TimerExists", "TimerInvalidName", "TimerNotFound",
	"TimeoutException", "distance_of_time_in_words", "processHostmask",
	"commandSplit", "argumentSplit", "commandlength", "functionHelp",
	"coerceToUnicode", "parseDateTime", "match_hostmask", "WDAY_MAP",
	"WDAY_SHORTMAP", "Mapping", "fetchone", "fetchall", "fetchmany",
	"pastehelper", "english_list", "URLREGEX",
)


def pastehelper(bot, basemsg, items=None, altmsg=None, sep=(", ","\n"), force=False, **kwargs):
	"""If using items, altmsg is an alternate string to interpolate with the items list."""
	try:
		tmsg = basemsg
		if not force:
			if items is not None:
				tmsg = basemsg % sep[0].join(items)
			if bot.checkSay(tmsg):
				return bot.say(tmsg)
		try:
			if items is not None:
				if altmsg: url = bot.getAddon("paste")(altmsg % sep[1].join(items), bot=bot, **kwargs)
				else: url = bot.getAddon("paste")(basemsg % sep[1].join(items), bot=bot, **kwargs)
			else:
				url = bot.getAddon("paste")(basemsg, bot=bot, **kwargs)
			if url:
				bot.say(basemsg % url)
			else:
				bot.say(basemsg % "Error: paste addon failure.")
		except AttributeError:
			if items is not None:
				bot.say(basemsg % "Error: too many entries to list and no paste addon.")
			else:
				bot.say(basemsg % "Error: too much data and no paste addon.")
	except Exception:
		# make sure contents of paste is at least dumped somewhere for recovery if need be.
		if items is not None:
			if altmsg: tmsg = altmsg % sep[1].join(items)
			else: tmsg = basemsg % sep[1].join(items)
		else:
			tmsg = basemsg
		print("ATTEMPTED PASTEHELPER MSG: %r" % tmsg)
		raise


def english_list(l):
	"""Stringify a list into 'arg1, arg2 and arg3', or 'arg1' if single-argument."""
	if not isinstance(l, (list, tuple)):
		l = (l, )
	if len(l) > 2:
		return "%s, and %s" % (", ".join(l[:-1]), l[-1])
	elif len(l) == 2:
		return "%s and %s" % (l[0], l[1])
	else:
		return l[0]

URLREGEX = recompile(r"""
\bhttps?\://					# schema
[\w.\:-]+						# domain
(?:/)?							# first path separator
(?:[\w%./_~!$&'()*+,;=:@-]+)?	# path
(?:\?[^ #\n\r]+)?				# querystring
(?:\#[^ #\n\r]+)?				# anchor (shouldn't be nested in querystring group)
""", IGNORECASE|VERBOSE)
