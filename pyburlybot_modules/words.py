# word tools
# 
from util import Mapping, functionHelp
from util.settings import ConfigException

REQUIRES = ("wordsapi")
WORD_API = None


def spelling(event, bot, skipSearch=False):
	""" spelling [query]. Returns spelling suggestions for query."""
	if not event.argument:
		return bot.say(functionHelp(spelling))
	suggestions = WORD_API.spell_check(event.argument, skipSearch)
	#TODO: Consider using googleapi to do a first pass
	if suggestions is None:
		return bot.say("\x02%s\x02 is spelt correct." % event.argument)
	else:
		if suggestions:
			return bot.say("Spelling suggestions: %s" % ", ".join(suggestions))
		else:
			try:
				suggestion, _ = bot.getModule("googleapi").google(event.argument)
				if suggestion: return bot.say("Google suggests: %s" % suggestion)
			except ConfigException:
				pass
			return bot.say("\x02%s\x02 is spelt wrong but I don't have any suggestions, sorry." % event.argument)


def dictionary(event, bot):
	""" dictionary [query]. Returns definitions for query."""
	if not event.argument: return bot.say(functionHelp(dictionary))
	defs = WORD_API.word_search(event.argument)
	if not defs:
		return spelling(event, bot, skipSearch=True)
	# pre process list
	output = []
	# could turn all this in to one huge comprehension but no.
	for p,ds in defs:
		ds = "; ".join((d[:-1] if d[:-1] == ":" else d for d in ds)) # strip trailing ":"
		output.append("%s: %s" % (p, ds))
	return bot.say(". ".join(output))


def synonym(event, bot):
	""" synonym [query]. Returns synonyms for query."""
	if not event.argument:
		return bot.say(functionHelp(synonym))
	syns = WORD_API.word_synonyms(event.argument)
	if syns is None:
		return spelling(event, bot, skipSearch=True)
	elif not syns:
		return bot.say("No synonyms found for \x02%s\x02" % event.argument)
	else:
		return bot.say("Synonyms for (%s): %s" % (event.argument, ", ".join(syns)))


def init(bot):
	global WORD_API # oh nooooooooooooooooo

	WORD_API = bot.getModule("wordsapi")

	return True


mappings = (Mapping(command=("dict", "d", "dictionary"), function=dictionary),
	Mapping(command=("spell", "sp", "spelling"), function=spelling),
	Mapping(command=("syn", "synonym", "thesaurus"), function=synonym),)
