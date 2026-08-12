# autorejoin
# delayed rejoin on kick
from twisted.internet import reactor
from util import Mapping

def autorejoin(event, bot):
	bot.notice(event.nick, "Berry sry")
	reactor.callFromThread(reactor.callLater, 5.0, bot.join, event.target)


#mappings to methods
mappings = (Mapping(("kickedFrom",), function=autorejoin),)
