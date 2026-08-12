# charinfo
# character info module. Information about unicode characters.

from unicodedata import name
from re import compile as compile_re, IGNORECASE

from util import Mapping, functionHelp

# build mini database of character names:
CHARACTER_DESC = []
for c in range(200000): # I think there is like 110000 unicode characters?? I don't know what ordinals they are though
	try: CHARACTER_DESC.append((c, name(chr(c))))
	except ValueError: pass
# U+0430 DESC (CHR)
RPLFORMAT = "U+%04X %s (%s)"
# hex(ord(u"\u30F5"))
REGHEX = compile_re("^[0-9A-F]{4}$", IGNORECASE)

def _getname(c):
	try: return name(c)
	except ValueError: return "NO NAME"
	

def funicode(event, bot):
	""" unicode [character(s)/description/hex]. Displays information about provided characters (limit of 3,) 
	or does a search on the character description or provides information on the character indexed by the given hexidecimal."""
	arg = event.argument
	if not arg:
		return bot.say(functionHelp(funicode))
	if REGHEX.match(arg):
		i = int(arg, 16)
		u = chr(i)
		return bot.say(RPLFORMAT % (i, _getname(u), u))
	elif len(arg) <= 3:
		output = []
		for u in arg:
			output.append(RPLFORMAT % (ord(u), _getname(u), u))
		return bot.say(", ".join(output))
	else:
		output = []
		for i, entry in CHARACTER_DESC:
			if len(output) > 8: break # could be lowered to improve performance
			if arg.lower() in entry.lower():
				output.append(RPLFORMAT % (i, entry, chr(i)))
		if output:
			return bot.say(", ".join(output))
		else:
			return bot.say("No characters found.")
		

mappings = (Mapping(command=("u", "unicode"), function=funicode),)
