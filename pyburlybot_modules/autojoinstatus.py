# basic autoop/voice/hop module

from util import Mapping, match_hostmask
from util.settings import ConfigException

OPTIONS = {
	"autoop" : (list, "List of hostmasks to autoop on. Use lowercase.", []),
	"autovoice" : (list, "List of hostmasks to autovoice on. Use lowercase.", []),
	"autohalf" : (list, "List of hostmasks to autohalf op on. Use lowercase.", []),
}

# TODO: Consider making custom IRCClient.mode function that can do this in one call
#		Something like takes user argument as list.
def dostatus(event, bot):
	target = event.target
	hostmask = event.hostmask
	chan = bot.state.channels.get(target)
	if not chan or (bot.nickname not in chan.ops): return
	modes = []
	target = target.lower()
	ops, voices, hops = bot.getOptions(("autoop", "autovoice", "autohalf"), module="autojoinstatus")
	for mask in ops:
		if match_hostmask(hostmask, mask):
			modes.append("o")
			break
	for mask in voices:
		if match_hostmask(hostmask, mask):
			modes.append("v")
			break
	for mask in hops:
		if match_hostmask(hostmask, mask):
			modes.append("h")
			break
	for mode in modes:
		bot.mode(target, True, mode, user=event.nick)

def init(bot):
	if not bot.getOption("enablestate"):
		raise ConfigException('autjoinstatus module requires "enablestate" option')
	return True

mappings = (Mapping(types=["userJoined"], function=dostatus),)
