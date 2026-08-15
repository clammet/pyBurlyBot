from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError, loads
from typing import Any

from lxml.etree import LxmlError
from lxml.html import fromstring

from util import Mapping, Timers, argumentSplit, distance_of_time_in_words, pastehelper
from util.timer import TimerExists
from util.event import Event
from util.http import HTTPError, HTTPClient
from util.types import BotLike


GDQ_SCHEDULE_URL = "https://gamesdonequick.com/schedule"
GDQ_STREAM_URL = "https://www.twitch.tv/gamesdonequick"
NEXT_DATA_PREFIX = "self.__next_f.push("
REQUEST_TIMEOUT = 15
TIMER_NAME = "gdq_timer"
LOOP_INTERVAL = 120.0
REPEAT_NOTIFY_TIME = 60 * 30
FORMAT = "{0}, GAME ({1}) IS AVAILABLE."
SCHEDULE_HTTP = HTTPClient(timeout=REQUEST_TIMEOUT)


class GDQScheduleError(RuntimeError):
    """The current GDQ schedule could not be downloaded or parsed."""


@dataclass(frozen=True)
class ScheduleRun:
    name: str
    category: str
    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        if self.category:
            return f"\x02{self.name}\x02 ({self.category})"
        return f"\x02{self.name}\x02"


def _schedule_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def parse_schedule_page(content: bytes | str) -> tuple[ScheduleRun, ...]:
    """Extract speedruns from the server-rendered Next.js schedule data."""
    document = fromstring(content)
    chunks: list[str] = []
    for script in document.xpath("//script/text()"):
        text = str(script).strip()
        if not text.startswith(NEXT_DATA_PREFIX) or not text.endswith(")"):
            continue
        try:
            flight_chunk = loads(text.removeprefix(NEXT_DATA_PREFIX).removesuffix(")"))
        except (JSONDecodeError, TypeError):
            continue
        if (
            isinstance(flight_chunk, list)
            and len(flight_chunk) == 2
            and flight_chunk[0] == 1
            and isinstance(flight_chunk[1], str)
        ):
            chunks.append(flight_chunk[1])

    runs: list[ScheduleRun] = []
    for line in "".join(chunks).splitlines():
        _, separator, value = line.partition(":")
        if not separator or not value.startswith("{"):
            continue
        try:
            record = loads(value)
        except (JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "speedrun":
            continue
        name = record.get("display_name") or record.get("name")
        category = record.get("category") or ""
        start = _schedule_datetime(record.get("starttime"))
        end = _schedule_datetime(record.get("endtime"))
        if (
            isinstance(name, str)
            and isinstance(category, str)
            and start is not None
            and end is not None
            and end >= start
        ):
            runs.append(ScheduleRun(name, category, start, end))
    return tuple(sorted(runs, key=lambda run: run.start))


def fetch_schedule() -> tuple[ScheduleRun, ...]:
    try:
        content = SCHEDULE_HTTP.get(GDQ_SCHEDULE_URL).body
    except (HTTPError, TimeoutError) as exc:
        raise GDQScheduleError(f"request failed: {exc}") from exc
    try:
        runs = parse_schedule_page(content)
    except (LxmlError, TypeError, ValueError) as exc:
        raise GDQScheduleError(f"invalid schedule response: {exc}") from exc
    if not runs:
        raise GDQScheduleError("the response contained no schedule runs")
    return runs


def schedule_status(
    runs: Sequence[ScheduleRun], now: datetime | None = None
) -> tuple[ScheduleRun | None, tuple[ScheduleRun, ...]]:
    if now is None:
        now = datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must include timezone information")
    current = next((run for run in runs if run.start <= now < run.end), None)
    upcoming = tuple(run for run in runs if run.start > now)[:3]
    return current, upcoming


def _relative_time(when: datetime, now: datetime) -> str:
    return distance_of_time_in_words(when.timestamp(), now.timestamp())


def format_schedule_status(
    runs: Sequence[ScheduleRun], now: datetime | None = None
) -> str:
    if now is None:
        now = datetime.now(UTC)
    current, upcoming = schedule_status(runs, now)
    parts: list[str] = []
    if current is None:
        parts.append("No GDQ event is currently running.")
    else:
        parts.append(
            f"Current: {current.label} (ends {_relative_time(current.end, now)})"
        )
    if upcoming:
        formatted = [
            f"{run.label} ({_relative_time(run.start, now)})" for run in upcoming
        ]
        parts.append("Upcoming: " + ", ".join(formatted))
    parts.append(f"\x0f| {GDQ_STREAM_URL} {GDQ_SCHEDULE_URL}")
    return " ".join(parts)


def gdq(event: Event, bot: BotLike) -> None:
    """gdq [gamename,~list,~del gamename]. Show gdq info. If gamename is provided, alert will be given when gamename is seen.
    gamename is searched in the time of the stream game, so "kirby" is possible for all kirby games."""
    action, value = argumentSplit(event.argument, 2)
    gamename = action if action and action.startswith("~") else event.argument
    if gamename:
        if event.isPM():
            chan_or_user = event.nick
        else:
            chan_or_user = event.target
        if gamename == "~list":
            alerts = bot.dbQuery(
                """SELECT game_text FROM gdq_alert
                    WHERE source=? AND source_name=? ORDER BY game_text;""",
                (chan_or_user, event.nick),
            )
            items = [row["game_text"] for row in alerts]
            if not items:
                return bot.say("You have no GDQ alerts here.")
            return pastehelper(bot, "GDQ alerts: %s", items=items, title="GDQ alerts")
        if gamename == "~del":
            if not value:
                return bot.say("Usage: gdq ~del game name")
            removed = bot.dbQuery(
                """DELETE FROM gdq_alert
                    WHERE source=? AND source_name=? AND game_text=?
                    RETURNING id;""",
                (chan_or_user, event.nick, value),
            )
            return bot.say(
                "GDQ alert deleted." if removed else "No matching GDQ alert was found."
            )
        if gamename.startswith("~"):
            return bot.say("GDQ actions: ~list, ~del game name")
        item = bot.dbQuery(
            """SELECT source, source_name, game_text
                FROM gdq_alert WHERE source=? AND source_name=? AND game_text=?; """,
            (chan_or_user, event.nick, gamename),
        )
        if item:
            bot.say("I'm already going to tell you about (%s)" % gamename)
            return
        bot.dbQuery(
            """INSERT INTO gdq_alert(source, source_name, game_text, notified_time) VALUES (?,?,?,?);""",
            (chan_or_user, event.nick, gamename, 0),
        )
        bot.say("I'll let you know when (%s) is on." % gamename)
        return

    try:
        runs = fetch_schedule()
    except GDQScheduleError as exc:
        print(f"GDQ schedule unavailable: {exc}")
        bot.say("GDQ schedule is temporarily unavailable. Try again later.")
        return
    bot.say(format_schedule_status(runs))


def check_games_callback(bot: BotLike) -> None:
    current_time = int(datetime.now(UTC).timestamp())
    timecheck = current_time - REPEAT_NOTIFY_TIME
    alerts = bot.dbQuery(
        """SELECT id, source, source_name, game_text, notified_time
            FROM gdq_alert WHERE notified_time<? ORDER BY notified_time;""",
        (timecheck,),
    )
    if not alerts:
        return
    try:
        runs = fetch_schedule()
    except GDQScheduleError as exc:
        print(f"GDQ alert check skipped: {exc}")
        return
    current, _ = schedule_status(runs)
    if current is None:
        return
    game = f"{current.name} {current.category}".lower()
    for alert in alerts:
        if alert["game_text"].lower() in game:
            claimed = bot.dbQuery(
                """UPDATE gdq_alert SET notified_time=?
                    WHERE id=? AND notified_time<? RETURNING id;""",
                (current_time, alert["id"], timecheck),
            )
            if not claimed:
                continue
            bot.sendmsg(
                alert["source"],
                FORMAT,
                strins=[alert["source_name"], alert["game_text"]],
            )


def setup_timer(bot: BotLike) -> None:
    try:
        Timers.addtimer(
            "%s:%s" % (TIMER_NAME, bot.network),
            LOOP_INTERVAL,
            check_games_callback,
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
        "gdq_alert",
        """CREATE TABLE gdq_alert(
            id INTEGER PRIMARY KEY,
            source TEXT,
            source_name TEXT,
             game_text TEXT,
            notified_time INTEGER
        );""",
    )
    bot.dbCheckCreateTable(
        "gdq_alert_idx",
        """CREATE INDEX gdq_alert_idx ON gdq_alert(notified_time);""",
    )
    bot.dbCheckCreateTable(
        "gdq_alert2_idx",
        """CREATE INDEX gdq_alert2_idx ON gdq_alert(source, source_name, game_text);""",
    )
    setup_timer(bot)
    return True


mappings = (Mapping(command=("gdq", "agdq", "sgdq"), function=gdq),)
