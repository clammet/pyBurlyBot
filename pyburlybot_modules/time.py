from util.event import Event
from util.types import BotLike
# time module

import re
from util import Mapping, pastehelper
from datetime import datetime
from functools import lru_cache
from time import gmtime, strftime
from calendar import timegm  # silly python... I just want UTC seconds
from zoneinfo import ZoneInfo, available_timezones

# You could do this without web based service but whatever, offloading is easier. Cloud7.0
REQUIRES = ("location", "users")

# utc / gmt / z with an optional ±H[:MM] offset
OFFSET_REGEX = re.compile(r"^(?:utc|gmt|z)(?:([+-])(\d{1,2})(?::?([0-5]\d))?)?$", re.I)


@lru_cache(maxsize=1)
def _zone_lookup() -> dict[str, str]:
    return {name.lower(): name for name in available_timezones()}


def _canonical_zone(query: str) -> str | None:
    return _zone_lookup().get(query.lower())


# reply for a timezone-spec query ("utc-11", "America/Chicago", "EST"), or
# None when the query isn't one and should fall through to geocoding
def _timezone_reply(query: str) -> str | None:
    query = query.strip()
    m = OFFSET_REGEX.match("".join(query.split()))
    if m:
        sign = -1 if m.group(1) == "-" else 1
        hours = int(m.group(2) or 0)
        minutes = int(m.group(3) or 0)
        if hours > 14:
            return None
        label = "UTC"
        if m.group(2):
            label += "%s%d" % ("-" if sign < 0 else "+", hours)
            if minutes:
                label += ":%02d" % minutes
        t = timegm(gmtime()) + sign * (hours * 3600 + minutes * 60)
        return "%s %s" % (strftime("%c", gmtime(t)), label)
    zone_name = _canonical_zone(query)
    if zone_name:
        local = datetime.now(ZoneInfo(zone_name))
        return "%s - %s (%s)" % (local.strftime("%c"), zone_name, local.tzname())
    return None


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
    # timezone specs ("utc-11", "EST", "Asia/Tokyo") beat geocoding, but a
    # known username of that name keeps precedence
    if event.argument and not bot.getModule("users").get_username(
        bot, event.argument, event.nick
    ):
        reply = _timezone_reply(event.argument)
        if reply:
            return bot.say(reply)
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
