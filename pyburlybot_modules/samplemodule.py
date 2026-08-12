from util.event import Event
from util.types import BotLike
#samplemodule
from re import compile as recompile
from util import Mapping, commandSplit, functionHelp

# options types for dynamic configuration setting via config module
# options not specified here will have no help text nor type checking and will not have defaults automatically set
OPTIONS = {
	"repeat" : (bool, "If true will repeat privmsg lines starting with '|'.", True),
	"ignorestarting" : (list, "Lines starting with items in this list will be ignored.", []),
}

def repeater(event: Event, bot: BotLike) -> None:
	#repeat things
	if bot.getOption("repeat", module="samplemodule"):
		bot.say("%s : %s" % (event.nick, event.msg))

def printer(event: Event, bot: BotLike) -> None:
	print(repr(event.msg))

def samplecommand(event: Event, bot: BotLike) -> None:
	""" samplecommand [option] [argument]. samplecommand will do things depending on what option is used. 
	Available option: something, dothing
	|samplecommand something [argument]: will output "something", and if argument is present, will follow.
	|samplecommand dothing [argument]: will output argument if it exists, followed by "dothing"
	"""
	#do some things
	command, args = commandSplit(event.argument)
	if command == "something":
		if args: bot.say("%s %s" % (command, args))
		else : bot.say("%s" % command)
	elif command == "dothing":
		if args: bot.say("%s %s" % (args, command))
		else : bot.say("%s" % command)
	else:
		bot.say(functionHelp(samplecommand))

#init should always be here to setup needed DB tables or objects or whatever
def init(bot: BotLike) -> bool:
	"""Do startup module things. This sample just checks if table exists. If not, creates it."""
	bot.dbCheckCreateTable('sample_table',
		'''create table sample_table(
			columnA,
			columnB
		);''')
	return True

#mappings to methods
mappings = (Mapping(types=["privmsged"], regex=recompile(r"^\|.*"), function=repeater),
	Mapping(command="samplecommand", function=samplecommand),
	Mapping(command="print", function=printer),)
