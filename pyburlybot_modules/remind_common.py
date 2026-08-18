"""Shared helpers for the tell and alert modules' remind-style commands.

Not a bot module itself: it has no mappings/init and is only imported by
tell.py and alert.py, so the module loader never activates it directly.
"""

from collections.abc import Iterable
from calendar import timegm
from collections import deque
from time import gmtime, localtime, struct_time
from types import ModuleType
from typing import Any

from util import argumentSplit, parseDateTime
from util.settings import ConfigException
from util.types import BotLike


def parse_remind_args(
    argument: str | None,
) -> tuple[str, str, str, str | None]:
    """Parse a "target datespec msg" argument (with the bare "tomorrow" datespec
    special case). Returns (status, target, dtime, msg) where status is one of
    "ok", "help" (no target), "time" (missing datespec), "msg" (missing msg);
    target and dtime are "" when absent."""
    target, dtime1, dtime2, msg = argumentSplit(argument, 4)
    if not target:
        return "help", "", "", msg
    if not dtime1:
        return "time", target, "", msg
    if dtime1.lower() == "tomorrow":
        # reparse is easiest way I guess... resolves #30 if need to readdress
        target, dtime1, msg = argumentSplit(argument, 3)
        dtime2 = ""
        if not target:
            return "help", "", "", msg
    elif not dtime2:
        return "time", target, "", msg
    if not msg:
        return "msg", target, "", msg
    return "ok", target, "%s %s" % (dtime1, dtime2), msg


def resolve_user_time(
    bot: BotLike, origuser: str, dtime: str
) -> tuple[float | None, float, int]:
    """Resolve a datespec in the requesting user's local timezone (using the
    location and googleapi modules when available). Returns
    (ntime, current_time, origintime) as UTC epoch seconds; ntime is None when
    the datespec could not be parsed."""
    locmod = None
    goomod = None
    timelocale = False
    try:
        locmod = bot.getModule("location")
        goomod = bot.getModule("googleapi")
        timelocale = True
    except ConfigException:
        pass

    origintime = timegm(gmtime())
    alocaltime = localtime(origintime)
    localoffset = timegm(alocaltime) - origintime
    t: struct_time = alocaltime
    tz: Any = None
    if locmod and goomod:
        loc = locmod.getlocation(bot.dbQuery, origuser)
        if not loc:
            timelocale = False
        else:
            tz = goomod.google_timezone(bot, loc[1], loc[2], origintime)
            if not tz:
                timelocale = False
            else:
                t = gmtime(origintime + tz[2] + tz[3])  # [2] dst [3] timezone offset
    ntime = parseDateTime(dtime, t)
    if not ntime:
        return None, 0.0, origintime

    # go on, change it. I dare you.
    if timelocale and tz is not None:
        current_time = timegm(t) - tz[2] - tz[3]
        ntime = ntime - tz[2] - tz[3]
    else:
        current_time = timegm(t) - localoffset
        ntime = ntime - localoffset
    return ntime, current_time, origintime


def _gather_group_users(
    users_module: ModuleType, bot: BotLike, s: str
) -> Iterable[tuple[str, str]]:
    return [(user, user) for user in users_module.expand_group(bot, s)]


def generate_users(
    bot: BotLike, s: str, nick: str, skipself: bool = True
) -> tuple[list[tuple[str, str]], list[str], bool, bool]:
    """Resolve a comma-separated list of users/groups (aware of names that
    themselves contain commas). Returns (users, unknown, dupes, hasself) where
    users is a list of (username, name_as_called)."""
    uset: set[str] = set()
    dupes = False
    users: list[tuple[str, str]] = []  # user,called
    unknown: list[str] = []
    targets = deque(s.split(","))
    hasself = False
    users_module = bot.getModule("users")

    def _collect(found: Iterable[tuple[str, str]]) -> None:
        nonlocal dupes, hasself
        for iu, it in found:
            if skipself and iu == nick:
                hasself = True
            elif iu in uset:
                dupes = True
            else:
                users.append((iu, it))
                uset.add(iu)

    while targets:
        t = targets.popleft()
        u = users_module.get_username(bot, t, nick)
        # check for user, then group (put user in list to make iteration easier)
        if u:
            u = ((u, t),)
        else:
            u = _gather_group_users(users_module, bot, t)

        if u:
            _collect(u)
        else:
            # Note: the following is silly code for allowing of groups/users with commas in them... silly.
            candidate_parts = [t]
            while not u and targets:
                candidate_parts.append(targets.popleft())
                u = users_module.get_username(bot, ",".join(candidate_parts), nick)
                if u:
                    u = ((u, ",".join(candidate_parts)),)
                else:
                    u = _gather_group_users(
                        users_module, bot, ",".join(candidate_parts)
                    )
            # at this point we either have u or ran out of deque, if latter, throw l[1:] back on queue
            if u:
                _collect(u)
            else:
                if candidate_parts[0]:
                    unknown.append(candidate_parts[0])
                remaining_parts = candidate_parts[1:]
                remaining_parts.reverse()
                targets.extendleft(remaining_parts)
    return users, unknown, dupes, hasself
