"""Shared helpers for the tell and alert modules' remind-style commands.

Not a bot module itself: it has no mappings/init and is only imported by
tell.py and alert.py, so the module loader never activates it directly.
"""

from collections.abc import Iterable
from calendar import timegm
from collections import deque
from datetime import UTC, datetime
from time import gmtime, localtime, mktime, struct_time
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from util import argumentSplit, parseDateTime
from util.settings import ConfigException
from util.types import BotLike


def _load_zone(zone_id: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(zone_id)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _walltime_to_epoch(ntime: int | float, zone: ZoneInfo | None) -> float:
    """Convert a walltime-as-UTC epoch (parseDateTime's working representation)
    to a real UTC epoch using the offset in effect at the *target* instant,
    so absolute datespecs land right across DST transitions. zone=None means
    the server's local timezone."""
    walltime = datetime.fromtimestamp(ntime, UTC).replace(tzinfo=None)
    if zone is not None:
        return walltime.replace(tzinfo=zone).timestamp()
    # datetime.timetuple() sets tm_isdst=-1: mktime resolves DST at the target
    return mktime(walltime.timetuple())


def _pull_spaced_units(dtime: str, msg: str) -> tuple[str, str]:
    """Support spaced datespecs ("in 3 days", "in 1h 30m") by pulling tokens
    from the start of msg into dtime while doing so changes what the datespec
    resolves to."""
    # fixed reference time so equal specs resolve equal across calls
    tref = timegm(gmtime())
    resolved = parseDateTime(dtime, tref)
    while msg:
        head, rest = argumentSplit(msg, 2)
        if not head:
            break
        candidate = "%s %s" % (dtime, head)
        cand_resolved = parseDateTime(candidate, tref)
        if cand_resolved is None or cand_resolved == resolved:
            break
        dtime, msg, resolved = candidate, rest or "", cand_resolved
    return dtime, msg


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
    dtime, msg = _pull_spaced_units(("%s %s" % (dtime1, dtime2)).strip(), msg)
    if not msg:
        return "msg", target, "", msg
    return "ok", target, dtime, msg


def resolve_user_time(
    bot: BotLike, origuser: str, dtime: str
) -> tuple[float | None, float, int]:
    """Resolve a datespec in the requesting user's local timezone (using the
    location module when available). Returns
    (ntime, current_time, origintime) as UTC epoch seconds; ntime is None when
    the datespec could not be parsed."""
    locmod = None
    try:
        locmod = bot.getModule("location")
    except ConfigException:
        pass

    origintime = timegm(gmtime())
    alocaltime = localtime(origintime)
    localoffset = timegm(alocaltime) - origintime
    t: struct_time = alocaltime
    tz: Any = None
    if locmod:
        tz = locmod.get_user_timezone(bot, origuser, origintime)
        if tz:
            t = gmtime(origintime + tz[2] + tz[3])  # [2] dst [3] timezone offset
    ntime = parseDateTime(dtime, t)
    if not ntime:
        return None, 0.0, origintime

    # Absolute specs name a walltime, so they convert with the offset at the
    # target instant (DST-correct); relative specs are durations from now, so
    # they keep plain offset arithmetic (elapsed time is what was asked for).
    spec = dtime.strip().lower()
    absolute = spec.startswith(("on", "at", "tomorrow"))
    # go on, change it. I dare you.
    if tz:
        current_time = timegm(t) - tz[2] - tz[3]
        zone = _load_zone(tz[0]) if absolute else None
        if zone is not None:
            ntime = _walltime_to_epoch(ntime, zone)
        else:
            # relative spec, or a zone id the tzdata doesn't know: use the
            # offsets sampled at command time
            ntime = ntime - tz[2] - tz[3]
    else:
        current_time = timegm(t) - localoffset
        if absolute:
            ntime = _walltime_to_epoch(ntime, None)
        else:
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
