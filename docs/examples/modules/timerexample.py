from util.event import Event
from util.types import BotLike
# Timer example; copy into pyburlybot_modules before enabling it.

from util import (
    Mapping,
    Timers,
    commandSplit,
    argumentSplit,
    TimerExists,
    TimerInvalidName,
    TimerNotFound,
)


# requires keyword arguments
def timercallback(
    bot: BotLike, channel: str | None = None, msg: str | None = None
) -> None:
    bot.sendmsg(channel, msg)


def timers(event: Event, bot: BotLike) -> None:
    command, args = commandSplit(event.argument)

    if command == "show":
        bot.say("Timers:")
        for timer in Timers.getTimers().values():
            bot.say(
                " - %s: reps = %s, delay = %s, f = %s"
                % (timer.name, timer.reps, timer.interval, timer.f)
            )

    elif command == "add":
        args = argumentSplit(args, 4)  # add timername delay reps msg
        timer_name, delay, repetitions, message = args
        if timer_name is None or delay is None or repetitions is None:
            bot.say(
                "Not enough arguments. Need: timername delay reps message (reps <= 0 means forever)"
            )
            return
        try:
            if Timers.addtimer(
                timer_name,
                float(delay),
                timercallback,
                reps=int(repetitions),
                msg=message,
                bot=bot,
                channel=event.target,
            ):
                bot.say("Timer added (%s)" % timer_name)
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


# mappings to methods
mappings = (Mapping(command="timers", function=timers),)
