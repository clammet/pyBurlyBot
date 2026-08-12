#timerexample.py

from util import Mapping, Timers, commandSplit, argumentSplit, TimerExists, TimerInvalidName, TimerNotFound

# requires keyword arguments
def timercallback(bot=None, channel=None, msg=None):
	bot.sendmsg(channel, msg)

def timers(event, bot):
	command, args = commandSplit(event.argument)
	
	if command == "show":
		bot.say("Timers:")
		for timer in Timers.getTimers().values():
			bot.say(" - %s: reps = %s, delay = %s, f = %s" % (timer.name, timer.reps, timer.interval, timer.f))
		
	elif command == "add":
		args = argumentSplit(args, 4) #add timername delay reps msg
		if not args:
			bot.say("Not enough arguments. Need: timername delay reps message (reps <= 0 means forever)")
			return
		try:
			if Timers.addtimer(args[0], float(args[1]), timercallback, reps=int(args[2]), msg=args[3], bot=bot, channel=event.target):
				bot.say("Timer added (%s)" % args[0])
			else:
				bot.say("Timer not added for some reason?")
		except TimerExists:
			bot.say("Timer not added because it exists already.")
		except TimerInvalidName:
			bot.say("Timer not added because it has an invalid name.")

	elif command == "stop":
		try: 
			Timers.deltimer(args)
			bot.say("Timer stopped (%s)" % args)
		except (TimerNotFound, TimerInvalidName):
			bot.say("Can't stop (%s) because timer not found or internal timer." % args)

#mappings to methods
mappings = (Mapping(command="timers", function=timers),)
