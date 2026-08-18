from util.event import Event
from util.types import BotLike
# time module

from util import Mapping, pastehelper
from time import gmtime, strftime
from calendar import timegm  # silly python... I just want UTC seconds

# You could do this without web based service but whatever, offloading is easier. Cloud7.0
REQUIRES = ("location", "users")


def _processTime(
    bot: BotLike, loc: tuple[str, float | str, float | str], group: bool = False
) -> tuple[int, str, tuple[str, str, int, int]] | None:
    name, lat, lon = loc
    t = timegm(gmtime())
    tz = bot.getModule("location").get_timezone(bot, lat, lon, t)
    if not group and not tz:
        return bot.say(
            "Can't find timezone information for (%s, %s, %s)" % (name, lat, lon)
        )
    elif group and not tz:
        return None
    # gdata["timeZoneId"], gdata["timeZoneName"], gdata["dstOffset"], gdata["rawOffset"]
    t = t + tz[2] + tz[3]
    # TODO: what time format??
    return t, name, tz


def ttime(event: Event, bot: BotLike) -> None:
    # attempt group first (because it's easier with current location module weirdness
    # (getLocationWithError needs rewrite with friendlier API)
    location_module = bot.getModule("location")
    users = bot.getModule("users").expand_group(bot, event.argument)
    if users:
        # process group request:

        if len(users) > 2:
            collate = True
        else:
            collate = False
        lines = []
        for u in users:
            success, data = location_module.getLocationWithError(
                bot, u, event.nick, group=True
            )
            if success:
                tdata = _processTime(bot, data, group=True)
                if tdata:
                    t, name, tz = tdata
                    if collate:
                        lines.append(
                            "(%s) %s - %s (%s-%s)"
                            % (u, strftime("%c", gmtime(t)), name, tz[0], tz[1])
                        )
                    else:
                        bot.say(
                            "(%s) %s - %s (%s-%s)"
                            % (u, strftime("%c", gmtime(t)), name, tz[0], tz[1])
                        )
                else:
                    if collate:
                        lines.append(
                            "(%s) Can't find timezone information for (%s, %s, %s)"
                            % (u, data[0], data[1], data[2])
                        )
                    else:
                        bot.say(
                            "(%s) Can't find timezone information for (%s, %s, %s)"
                            % (u, data[0], data[1], data[2])
                        )
            else:
                if collate:
                    lines.append(data)
                else:
                    bot.say(data)
        if collate:
            msg = "Times for group (%s): %%s" % event.argument
            pastehelper(bot, msg, items=lines, altmsg="%s", force=True, title=msg[:-4])
        return
    # continue if only single user:
    loc = location_module.getLocationWithError(bot, event.argument, event.nick)
    if not loc:
        return
    tdata = _processTime(bot, loc)
    # lookup location offset
    # apply to : timegm(gmtime())
    if tdata:
        t, name, tz = tdata
        bot.say("%s - %s (%s-%s)" % (strftime("%c", gmtime(t)), name, tz[0], tz[1]))


def init(bot: BotLike) -> bool:
    return True


mappings = (Mapping(command=("time", "t"), function=ttime),)
