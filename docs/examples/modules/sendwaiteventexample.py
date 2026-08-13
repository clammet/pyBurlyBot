from util.event import Event
from util.types import BotLike
# Wait-event example; copy into pyburlybot_modules before enabling it.

# example on how to send and then wait on events

from util import Mapping, TimeoutException


def waitexample(event: Event, bot: BotLike) -> None:
    count = 0
    try:
        for received_event in bot.send_and_wait(
            "noticed", f=bot.notice, fargs=(event.nick, "sending...")
        ):
            bot.say("Received: %s" % received_event.msg)
            count += 1
            if count > 1:
                raise Exception()
    except TimeoutException:
        print("TIMEOUT!")
    print("bailed generator")


# mappings to methods
mappings = (Mapping(types=["privmsged"], command="waitexample", function=waitexample),)
