#sendwaiteventexample.py

#example on how to send and then wait on events

from util import Mapping, TimeoutException
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread

def waitexample(event, bot):
	count = 0
	try:
		for event in bot.send_and_wait("noticed", f=bot.notice, fargs=(event.nick, "sending...")):
			bot.say("Recieved: %s" % event.msg)
			count += 1
			if count > 1: raise Exception()
	except TimeoutException:
		print("TIMEOUT!")
	print("bailed generator")
	
def printwaits(s):
	print(s.dispatcher.waitmap)

# never ever do something like this please, please. This is debugging example.	
def waitlist(event, bot):
	blockingCallFromThread(reactor, printwaits, bot._settings)
	bot.say("done.")

#mappings to methods
mappings = (Mapping(types=["privmsged"], command="waitexample", function=waitexample),
	Mapping(types=["privmsged"], command="waitlist", function=waitlist))
