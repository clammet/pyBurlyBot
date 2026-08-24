from collections.abc import Sequence
import sqlite3
from util.event import Event
from util.types import BotLike
from util.db import Query

# alert module
from time import gmtime
from util import Timers, TimerExists
from calendar import timegm

from util import (
    Mapping,
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

TIMER_NAME = "alert_timer"
REQUIRES = ("users",)
# Seconds
LOOP_INTERVAL = 30.0

MULTIUSER = " %s someone once is enough."
RPL_ALERT_FORMAT = "%s: I will alert %s about that %s.%s%s"
ALERT_FORMAT = "{0}, alert from {1}: {2} - set {3}."
SELF_ALERT_FORMAT = "{0}, alert: {1} - set {2}."
UNKNOWN = " Don't know (%s)."

MAX_REMIND_TIME = 31540000  # 1 year


def _timer_name(bot: BotLike, suffix: str = "") -> str:
    return "%s:%s%s" % (TIMER_NAME, bot.network, suffix)


def check_alerts_callback(bot: BotLike) -> None:
    current_time = int(timegm(gmtime()))
    timecheck = current_time + int(LOOP_INTERVAL)
    # This seems like it might be a bit of a waste. But it should stop the rare occurance of "double tell delivery" (I've only seen it happen once.)
    alerts = bot.dbQuery(
        """SELECT id, target_user, alert_time, created_time, source, source_user, msg
            FROM alert WHERE delivered=0 AND alert_time<? ORDER BY alert_time;""",
        (timecheck,),
    )

    deliver_now: dict[str, list[sqlite3.Row]] = {}
    deliver_soon: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for a in alerts:
        chan_or_user = a["source"].lower()
        delay = a["alert_time"] - current_time
        if delay <= 0:
            deliver_now.setdefault(chan_or_user, []).append(a)
        else:
            # Schedule per distinct due-time so no alert is delivered early
            deliver_soon.setdefault((chan_or_user, a["alert_time"]), []).append(a)

    for chan_or_user, alerts in deliver_now.items():
        deliver_alerts(chan_or_user, alerts, bot)

    for (chan_or_user, alert_time), alerts in deliver_soon.items():
        delay = alert_time - current_time
        ids = "_".join(str(x["id"]) for x in alerts)
        timer_name = _timer_name(bot, ":" + ids)
        try:
            Timers.addtimer(
                timer_name,
                delay,
                deliver_alerts,
                reps=1,
                chan_or_user=chan_or_user,
                alerts=alerts,
                bot=bot,
            )
        except TimerExists:
            pass


def deliver_alerts(
    chan_or_user: str | None = None,
    alerts: Sequence[sqlite3.Row] | None = None,
    bot: BotLike | None = None,
) -> None:
    if not bot:
        return
    if not alerts:
        return
    current_time = int(timegm(gmtime()))

    row_ids = [int(alert["id"]) for alert in alerts]
    # Atomically claim before sending for at-most-once delivery. Competing timers
    # will receive no rows from RETURNING and therefore cannot duplicate alerts.
    claim_results = bot.dbBatch(
        tuple(
            (
                """UPDATE alert SET delivered=1 WHERE delivered=0 AND id=?
                    RETURNING id, target_user, alert_time, created_time,
                        source, source_user, msg;""",
                (row_id,),
            )
            for row_id in row_ids
        )
    )
    alerts = [alert for result in claim_results for alert in result]
    alerts.sort(key=lambda row: (row["alert_time"], row["id"]))

    if not alerts:
        return

    collate = False
    lines: list[str] | None = None
    if len(alerts) > 3:
        collate = True
        lines = []

    for a in alerts:
        receiving_user = a["target_user"]
        source_user = a["source_user"]
        if source_user:
            data = [
                a["target_user"],
                source_user,
                a["msg"],
                distance_of_time_in_words(a["created_time"], current_time),
            ]
            fmt = ALERT_FORMAT
        else:
            data = [
                a["target_user"],
                a["msg"],
                distance_of_time_in_words(a["created_time"], current_time),
            ]
            fmt = SELF_ALERT_FORMAT

        if collate and lines is not None:
            lines.append(fmt.format(*data))
        else:
            bot.sendmsg(chan_or_user, fmt, strins=data, fcfs=True)

    if collate and lines is not None:
        msg = "Alerts for (%s): %%s" % receiving_user
        title = "Alerts for (%s)" % receiving_user
        pastehelper(
            bot,
            msg,
            items=lines,
            altmsg="%s",
            force=True,
            target=chan_or_user,
            title=title,
        )


def alert(event: Event, bot: BotLike) -> None:
    """alert target datespec msg. Alert a user <target> about a message <msg> at <datespec> time.
    datespec can be relative (in) or calendar/day based (on), e.g. 'in 5 minutes'"""
    status, target, dtime, msg = parse_remind_args(event.argument)
    if status == "help":
        return bot.say(functionHelp(alert))
    if status == "time":
        return bot.say("Need time to alert.")
    if status == "msg":
        return bot.say("Need something to alert (%s)" % target)

    origuser = bot.getModule("users").get_username(bot, event.nick) or event.nick or ""
    users, unknown, dupes, _ = generate_users(bot, target, origuser, False)

    if not users:
        return bot.say("Sorry, don't know (%s)." % target)

    # user location aware destination times
    ntime, current_time, origin_time = resolve_user_time(bot, origuser, dtime)
    if ntime is None:
        return bot.say("Don't know what time and/or day and/or date (%s) is." % dtime)

    if ntime < current_time or ntime > (current_time + MAX_REMIND_TIME):
        return bot.say("Don't sass me with your back to the future alerts.")
    if ntime < (current_time + 5):
        return bot.say("2fast")

    if event.isPM():
        chan_or_user = event.nick
    else:
        chan_or_user = event.target

    targets = []
    for user, orig_nick in users:
        if user == origuser:
            source_user = None
        else:
            source_user = event.nick

        bot.dbQuery(
            """INSERT INTO alert(target_user, alert_time, created_time, source, source_user, msg) VALUES (?,?,?,?,?,?);""",
            (user, int(ntime), int(origin_time), chan_or_user, source_user, msg),
        )

        if not source_user:
            targets.append("you")
        else:
            targets.append(orig_nick)
    if ntime < (current_time + LOOP_INTERVAL):
        Timers.restarttimer(_timer_name(bot))
    bot.say(
        RPL_ALERT_FORMAT
        % (
            event.nick,
            english_list(targets),
            distance_of_time_in_words(ntime, current_time),
            UNKNOWN % english_list(unknown) if unknown else "",
            MULTIUSER % "Alerting" if dupes else "",
        )
    )


def _user_rename(old: str, new: str) -> tuple[Query, ...]:
    return (("""UPDATE alert SET target_user=? WHERE target_user=?;""", (new, old)),)


def setup_timer(event: Event, bot: BotLike) -> None:
    try:
        Timers.addtimer(
            _timer_name(bot),
            LOOP_INTERVAL,
            check_alerts_callback,
            reps=-1,
            startnow=False,
            bot=bot,
        )
    except TimerExists:
        pass


def unload() -> None:
    Timers._delPrefix(TIMER_NAME + ":")


def init(bot: BotLike) -> bool:
    bot.dbCheckCreateTable(
        "alert",
        """CREATE TABLE alert(
            id INTEGER PRIMARY KEY,
            delivered INTEGER DEFAULT 0,
            target_user TEXT COLLATE NOCASE,
            source TEXT,
            source_user TEXT,
            alert_time INTEGER,
            created_time INTEGER,
            msg TEXT
        );""",
    )

    bot.dbCheckCreateTable(
        "alert_deliv_idx",
        """CREATE INDEX alert_deliv_idx ON alert(delivered, alert_time);""",
    )

    # Modules storing "users" in their own tables should register to be notified when a username is changed (by the alias module)
    bot.getModule("users").REGISTER_UPDATE(bot.network, _user_rename)
    return True


mappings = (
    Mapping(command=("alert", "alarm"), function=alert),
    Mapping(types=("signedon",), function=setup_timer),
)
