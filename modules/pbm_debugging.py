#debugging.py

#some commands to facilitate debugging
# you shouldn't enable this

from util import Mapping
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread

def doeval(bot, event):
	try:
		exec(event.argument)
		return None
	except Exception as e:
		return "%s : %s" % (type(e).__name__, e.message)


# WARNING: DO NOT CALL A METHOD THAT CALLS "blockingCallFromThread", you will have bad time and freeze bot.
def admin_runeval(event, bot):
	r = blockingCallFromThread(reactor, doeval, bot, event)
	if r:
		bot.say(r)
	else:
		bot.say("Done.")


def admin_flood(event, bot):
	for x in range(7):
		bot.say("Hello %s" % x)

#mappings to methods
mappings = (Mapping(command="eval", function=admin_runeval, admin=True),
	Mapping(command="flood", function=admin_flood, admin=True),)
