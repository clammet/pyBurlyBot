from collections.abc import Callable, Iterable
from util.event import Event
from util.types import BotLike
# nicktools

# Handles identifying to nickserv (when not using client certificate fingerprints)
# and restoring original nickname when available.
# Currently only implemented for Rizon's nickserv implementation.

# SPECIAL NOTE:
# Rizon does not send a NICK event when connecting so this module uses the "signedOn" BurlyBot event
# to check intial nick. If a different server does send a NICK event on connection, then mappings will
# need to be dynamically managed for that type of server else you'll get two simutanious "nickCheckAndRecover"
# actions.

from util import Mapping, Timers, TimerExists, TimerNotFound

from twisted.internet import reactor

OPTIONS = {
	"restorenick" : (int, "Attempt to restore nickname every <checkevery> seconds. 0 = don't attempt, 1 = attempt,"\
		" >1 forcefully recover the nick.", 1),
	"checkevery" : (int, "Time in seconds to check if we are using our nickname", 90),
}

def nickCheckAndRecover(event: Event | None=None, bot: BotLike | None=None) -> None:
	assert bot is not None
	snick = bot.getOption("nick")
	snickpass = bot.getOption("nickservpass")
	srestorenick = bot.getOption("restorenick", module="nicktools")
	if bot.nickname != snick:
		if srestorenick > 1 and snickpass:
			#aggressive nick restore attempt kill old nick
			# need to staggered delay these calls because it doesn't work if you just spam them
			# TODO: Should we somehow have a convenience for the following makeshift?
			# TODO: Should these values be higher? To allow for latency spike? (Currently works with latency of ~200ms)
			# (Ideally it would happen before bot joins channels. Join is delayed further if we don't have desired nick.)
			bot.sendmsg("nickserv", "RECOVER %s %s" % (snick, snickpass))
			reactor.callFromThread(reactor.callLater, 0.3, bot._botinst.sendmsg, "nickserv", "RELEASE %s %s" % (snick, snickpass))
			# delayed call for setting nick after ghost. (and hope we didn't DC in the meantime??
			reactor.callFromThread(reactor.callLater, 0.6, bot._botinst.setNick, snick)
		else:
			bot.setNick(snick)

def delayedJoin(joinfunc: Callable[..., None],
	channels: Iterable[tuple[str, ...]]) -> None:
	for chan in channels:
		joinfunc(*chan)

def identify(bot: BotLike, snick: str | None=None) -> bool:
	passwd = bot.getOption("nickservpass")
	if not snick: snick = bot.getOption("nick")
	if bot.nickname == snick and passwd:
		if not bot.getOption("cert"): bot.sendmsg("nickserv", "identify %s" % passwd)
		# send notice to self to see if prefix changed, allow for some latency:
		# special magic to not get the wrapped bot function for call inside reactor (Don't do this.)
		# this may do odd things if bot disconnects while preJoin (or nickChanged) got sent.
		reactor.callFromThread(reactor.callLater, 0.8, bot._botinst.notice, bot.nickname, "\x1b")
		return True
	return False

def preJoin(event: Event, bot: BotLike) -> None:
	if identify(bot):
		# if identified on connect, join sooner
		reactor.callFromThread(reactor.callLater, 1.5, delayedJoin, bot._botinst.join, bot.getOption("channels"))
	else:
		# if not identified on connect, join sometime after nick reclaim hopefully happens
		reactor.callFromThread(reactor.callLater, 5.5, delayedJoin, bot._botinst.join, bot.getOption("channels"))

def nickChanged(event: Event, bot: BotLike) -> None:
	if bot.getOption("restorenick", module="nicktools"):
		snick = bot.getOption("nick")
		if event.newname != snick:
			# start timer to checkandrecov
			try: Timers.addtimer("NICKTOOLS_%s" % bot.network, float(bot.getOption("checkevery", module="nicktools")), 
				nickCheckAndRecover, reps=-1, bot=bot )
			except TimerExists: pass
		else:
			# have desired nick, delete check timers
			try: Timers.deltimer("NICKTOOLS_%s" % bot.network)
			except TimerNotFound: pass
			# TODO: like below, when state can track +r, this should be checked before attempting identify
			identify(bot, snick)

def init(bot: BotLike) -> bool:
	return True
	
def unload() -> None:
	Timers._delPrefix("NICKTOOLS_") # use non blocking call version since unload is called in the reactor
				
mappings = (Mapping(types=["preJoin"], function=preJoin), Mapping(types=["signedOn"], function=nickCheckAndRecover),
	Mapping(types=["nickChanged"], function=nickChanged),)
