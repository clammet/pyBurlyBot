# help module
from util import Mapping, functionHelp, argumentSplit
from util.helpers import isIterable

from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread

def _filter_mappings(bot, pm=False, cmd=None):
	mappings = bot._settings.dispatcher._getCommandMappings(cmd)
	if not cmd:
		# flattening the nested mappings http://stackoverflow.com/a/952952
		# "incomprehensible list comprehensions", lol
		mappings = (item for sublist in mappings for item in sublist)
	
	return [mapping for mapping in mappings if not mapping.hidden and (mapping.admin and pm and bot._isadmin() or not mapping.admin)]

def list_commands(bot, pm=False):
	cmds = set()
	for mapping in blockingCallFromThread(reactor, _filter_mappings, bot, pm):
		cmds.add(mapping.command[0])
	cmds = list(cmds)
	cmds.sort()
	bot.say(" ".join(cmds))

def help(event, bot):
	""" help [argument].  If argument is specified, get the help string for that command.
	Otherwise list all commands (same as commands function).
	"""
	cmd, arg = argumentSplit(event.argument, 2)
	# other modules should probably not do this:
	if cmd:
		cmd_mappings = blockingCallFromThread(reactor, _filter_mappings, bot, event.isPM, cmd)
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
						if isIterable(command) and len(command) > 1:
							bot.say("%s Aliases: %s" % (h, ", ".join(command)))
						else:
							bot.say(h)
					else:
						bot.say("No help for (%s) available." % cmd)
		else:
			bot.say("Command %s not found." % cmd)
	else:
		list_commands(bot, event.isPM())

def commands(event, bot):
	""" commands.  List available pyBurlyBot commands by their primary name.
	"""
	list_commands(bot, event.isPM())



mappings = (Mapping(command="help", function=help),
			Mapping(command="commands", function=commands))
