import sqlite3
from util.event import Event
from util.types import BotLike
from util.db import Query

# tell module
from time import gmtime
from calendar import timegm  # silly python... I just want UTC seconds

from util import (
    Mapping,
    argumentSplit,
    functionHelp,
    distance_of_time_in_words,
    pastehelper,
    english_list,
)
from pyburlybot_modules.remind_common import (
    generate_users,
    parse_remind_args,
    resolve_user_time,
)

REQUIRES = ("users",)

# nick: <source> msg - time
TELLFORMAT = "{0}: <{1}> {2} - {3}"
# nick: I'll pass that on when target is around.
RPLFORMAT = "%s: I'll %s when %s %s around.%s%s%s"
PASSON = "pass that on"
ASKTHAT = "ask that"
UNKNOWN = " Don't know (%s)."
URSELF = " Use notepad for yourself."
MULTIUSER = " %s someone once is enough."
# nick: I will remind target about that in timespec.
RPLREMINDFORMAT = "%s: I will remind %s about that %s.%s%s"
# TARGET, reminder from SOURCE: MSG - set TELLTIME, arrived TOLDTIME.
REMINDFORMAT = "{0}, reminder from {1}: {2} - set {3}, arrived {4}."
SELFREMINDFORMAT = "{0}, reminder: {1} - set {2}, arrived {3}."

MAX_REMIND_TIME = 157700000  # 5 year


def _render_tells(
    bot: BotLike, nick: str | None, tells: list[sqlite3.Row], toldtime: int
) -> None:
    collate = len(tells) > 3
    lines: list[str] = []
    for tell in tells:
        if tell["remind"]:
            source = tell["source"]
            if source:
                data = [
                    nick,
                    source,
                    tell["msg"],
                    distance_of_time_in_words(tell["origintime"], toldtime),
                    distance_of_time_in_words(
                        tell["telltime"], toldtime, suffix="late"
                    ),
                ]
                fmt = REMINDFORMAT
            else:
                data = [
                    nick,
                    tell["msg"],
                    distance_of_time_in_words(tell["origintime"], toldtime),
                    distance_of_time_in_words(
                        tell["telltime"], toldtime, suffix="late"
                    ),
                ]
                fmt = SELFREMINDFORMAT
        else:
            data = [
                nick,
                tell["source"],
                tell["msg"],
                distance_of_time_in_words(tell["telltime"], toldtime),
            ]
            fmt = TELLFORMAT
        if collate:
            lines.append(fmt.format(*data))
        else:
            bot.say(fmt, strins=data, fcfs=True)
    if collate:
        msg = "Tells/reminds for (%s): %%s" % nick
        title = "Tells/reminds for (%s)" % nick
        pastehelper(bot, msg, items=lines, altmsg="%s", force=True, title=title)


def deliver_tell(event: Event, bot: BotLike) -> None:
    # resolve_nick over get_username: one db call, and no user-table check that
    # could miss a first-time user whose row user_update hasn't written yet
    user = bot.getModule("users").resolve_nick(bot, event.nick) or event.nick
    toldtime = int(timegm(gmtime()))
    # Claim before sending. This intentionally provides at-most-once delivery:
    # a process failure after this statement can lose a tell, but cannot duplicate it.
    tells = bot.dbQuery(
        """UPDATE tell SET delivered=1, toldtime=?
            WHERE id IN (
                SELECT id FROM tell
                WHERE user=? AND delivered=0 AND telltime<?
            )
            RETURNING id, source, telltime, origintime, remind, msg;""",
        (toldtime, user, toldtime),
    )
    tells.sort(key=lambda row: (row["telltime"], row["id"]))
    if tells:
        _render_tells(bot, event.nick, tells, toldtime)


def tells(event: Event, bot: BotLike) -> None:
    """tells [n]. Repeats your nth most recent batch of delivered tells/reminds (default: the last batch)."""
    user = bot.getModule("users").resolve_nick(bot, event.nick) or event.nick
    n = 1
    if event.argument:
        try:
            # allow ".tells -2" to mean the same as ".tells 2"
            n = abs(int(event.argument.strip()))
        except ValueError:
            return bot.say(functionHelp(tells))
        n = max(n, 1)
    # batches are delivery groups: every tell delivered in one go shares a toldtime
    batch = bot.dbQuery(
        """SELECT id, source, telltime, origintime, toldtime, remind, msg
            FROM tell WHERE user=? AND delivered=1 AND toldtime=(
                SELECT DISTINCT toldtime FROM tell
                WHERE user=? AND delivered=1
                ORDER BY toldtime DESC LIMIT 1 OFFSET ?);""",
        (user, user, n - 1),
    )
    if not batch:
        return bot.say(
            "No delivered tells found%s." % ("" if n == 1 else " that far back")
        )
    batch.sort(key=lambda row: (row["telltime"], row["id"]))
    _render_tells(bot, event.nick, batch, batch[0]["toldtime"])


def tell(event: Event, bot: BotLike) -> None:
    """tell target msg. Will tell a user <target> a message <msg>."""
    target, msg = argumentSplit(event.argument, 2)
    if not target:
        return bot.say(functionHelp(tell))
    if not msg:
        return bot.say("Need something to tell (%s)" % target)
    caller = bot.getModule("users").get_username(bot, event.nick) or event.nick or ""
    users, unknown, dupes, hasself = generate_users(bot, target, caller)

    if not users:
        if hasself:
            return bot.say("Use notepad.")
        else:
            return bot.say("Sorry, don't know (%s)." % target)

    cmd = (event.command or "").lower()

    targets = []
    for user, orig_nick in users:
        # cmd user msg
        imsg = "%s %s %s" % (cmd, orig_nick, msg)
        # TODO: do we do an alias lookup on event.nick also?
        bot.dbQuery(
            """INSERT INTO tell(user, telltime, source, msg) VALUES (?,?,?,?);""",
            (user, int(timegm(gmtime())), event.nick, imsg),
        )
        targets.append(orig_nick)
    if len(users) > 1:
        bot.say(
            RPLFORMAT
            % (
                event.nick,
                PASSON if cmd == "tell" else ASKTHAT,
                english_list(targets),
                "are",
                UNKNOWN % english_list(unknown) if unknown else "",
                URSELF if hasself else "",
                MULTIUSER % "Telling" if dupes else "",
            )
        )
    else:
        bot.say(
            RPLFORMAT
            % (
                event.nick,
                PASSON if cmd == "tell" else ASKTHAT,
                english_list(targets),
                "is",
                UNKNOWN % english_list(unknown) if unknown else "",
                URSELF if hasself else "",
                MULTIUSER % "Telling" if dupes else "",
            )
        )


def remind(event: Event, bot: BotLike) -> None:
    """remind target datespec msg. Will remind a user <target> about a message <msg> at <datespec> time.
    datespec can be relative (in) or calendar/day based (on), e.g. 'in 5 minutes'"""
    status, target, dtime, msg = parse_remind_args(event.argument)
    if status == "help":
        return bot.say(functionHelp(remind))
    if status == "time":
        return bot.say("Need time to remind.")
    if status == "msg":
        return bot.say("Need something to remind (%s)" % target)

    origuser = bot.getModule("users").get_username(bot, event.nick) or event.nick or ""
    users, unknown, dupes, _ = generate_users(bot, target, origuser, False)

    if not users:
        return bot.say("Sorry, don't know (%s)." % target)

    # user location aware destination times
    ntime, current_time, origintime = resolve_user_time(bot, origuser, dtime)
    if ntime is None:
        return bot.say("Don't know what time and/or day and/or date (%s) is." % dtime)

    if ntime < current_time or ntime > current_time + MAX_REMIND_TIME:
        return bot.say("Don't sass me with your back to the future reminds.")

    targets = []
    for user, orig_nick in users:
        if user == origuser:
            source = None
        else:
            source = event.nick
        bot.dbQuery(
            """INSERT INTO tell(user, telltime, origintime, remind, source, msg) VALUES (?,?,?,?,?,?);""",
            (user, int(ntime), int(origintime), 1, source, msg),
        )
        if not source:
            targets.append("you")
        else:
            targets.append(orig_nick)
    bot.say(
        RPLREMINDFORMAT
        % (
            event.nick,
            english_list(targets),
            distance_of_time_in_words(ntime, current_time),
            UNKNOWN % english_list(unknown) if unknown else "",
            MULTIUSER % "Reminding" if dupes else "",
        )
    )


def _user_rename(old: str, new: str) -> tuple[Query, ...]:
    return (("""UPDATE tell SET user=? WHERE user=?;""", (new, old)),)


def init(bot: BotLike) -> bool:
    bot.dbCheckCreateTable(
        "tell",
        """CREATE TABLE tell(
            id INTEGER PRIMARY KEY,
            delivered INTEGER DEFAULT 0,
            user TEXT COLLATE NOCASE,
            telltime INTEGER,
            origintime INTEGER,
            toldtime INTEGER,
            remind INTEGER DEFAULT 0,
            source TEXT,
            msg TEXT
        );""",
    )
    # I am bad at indexes.
    bot.dbCheckCreateTable(
        "tell_deliv_idx",
        """CREATE INDEX tell_deliv_idx ON tell(user, delivered, telltime);""",
    )
    # delivered-batch lookups for .tells
    bot.dbCheckCreateTable(
        "tell_told_idx",
        """CREATE INDEX tell_told_idx ON tell(user, delivered, toldtime);""",
    )

    # Modules storing "users" in their own tables should register to be notified when a username is changed (by the alias module)
    bot.getModule("users").REGISTER_UPDATE(bot.network, _user_rename)
    return True


mappings = (
    Mapping(types=["privmsged"], function=deliver_tell),
    Mapping(command=("tell", "ask"), function=tell),
    Mapping(command="remind", function=remind),
    Mapping(command=("tells", "lasttells"), function=tells),
)
