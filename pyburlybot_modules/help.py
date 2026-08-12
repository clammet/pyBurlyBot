from util.event import Event
from util.types import BotLike
from typing import Any, cast
# help module
from util import Mapping, functionHelp, argumentSplit
from util.helpers import isIterable

from twisted.internet import reactor as _reactor
from twisted.internet.threads import blockingCallFromThread

reactor: Any = _reactor

def _filter_mappings(bot: BotLike, pm: bool=False,
	cmd: str | None=None) -> list[Mapping]:
	mappings = bot._settings.dispatcher._getCommandMappings(cmd)
	if not cmd:
		# flattening the nested mappings http://stackoverflow.com/a/952952
		# "incomprehensible list comprehensions", lol
		mappings = (item for sublist in cast(list[list[Mapping]], mappings) for item in sublist)
	
	return [mapping for mapping in cast(Any, mappings) if not mapping.hidden and (mapping.admin and pm and bot._isadmin() or not mapping.admin)]

def list_commands(bot: BotLike, pm: bool=False) -> None:
	cmds: set[str] = set()
	for mapping in blockingCallFromThread(reactor, _filter_mappings, bot, pm):
		if mapping.command:
			cmds.add(mapping.command[0])
	bot.say(" ".join(sorted(cmds)))

def help(event: Event, bot: BotLike) -> None:
	""" help [argument].  If argument is specified, get the help string for that command.
	Otherwise list all commands (same as commands function).
	"""
	cmd, arg = argumentSplit(event.argument, 2)
	# other modules should probably not do this:
	if cmd:
		cmd_mappings: list[Mapping] = blockingCallFromThread(reactor, _filter_mappings, bot, event.isPM(), cmd)
		if cmd_mappings:
			for mapping in cmd_mappings:
				if arg:
					h = functionHelp(mapping.function, arg)
					if h: bot.say(h)
					else: bot.say("No help for (%s) available." % cmd)
				else:
					h = functionHelp(mapping.function)
					if h:
						command = mapping.command
						if command and isIterable(command) and len(command) > 1:
							bot.say("%s Aliases: %s" % (h, ", ".join(command)))
						else:
							bot.say(h)
					else:
						bot.say("No help for (%s) available." % cmd)
		else:
			bot.say("Command %s not found." % cmd)
	else:
		list_commands(bot, event.isPM())

def commands(event: Event, bot: BotLike) -> None:
	""" commands.  List available pyBurlyBot commands by their primary name.
	"""
	list_commands(bot, event.isPM())



mappings = (Mapping(command="help", function=help),
			Mapping(command="commands", function=commands))
