"""Channel-linked When availability."""

from logging import getLogger
from math import isfinite

from util import Mapping, fetchone, functionHelp, pastehelper
from util.event import Event
from util.helpers import irc_casefold
from util.http import HTTPError
from util.irctools import strip_control_characters
from util.types import BotLike

REQUIRES = "whenapi"
OPTIONS = {
    "lookahead_hours": (
        float,
        "Include the next availability starting within this many hours (0-168).",
        1.7,
    ),
}
log = getLogger(__name__)


def _duration(hours: float) -> str:
    whole_hours, minutes = divmod(round(hours * 60), 60)
    return "%dh%dm" % (whole_hours, minutes)


def _name(value: str) -> str:
    return (
        " ".join(
            "".join(
                char
                for char in strip_control_characters(value)
                if char.isprintable() or char.isspace()
            ).split()
        )
        or "Unknown"
    )


def when(event: Event, bot: BotLike) -> None:
    """when [~link <URL>|~unlink]. Show current and upcoming availability on this channel's linked schedule."""
    if not event.target or event.isPM():
        return bot.say(
            "Use this command in the channel whose schedule you want to check or link."
        )
    key = (bot.network, irc_casefold(event.target))
    argument = (event.argument or "").strip()
    if argument == "~unlink":
        bot.dbQuery("DELETE FROM when_links WHERE network = ? AND channel = ?", key)
        return bot.say("Schedule unlinked for this channel.")
    command, _, url = argument.partition(" ")
    linking = command == "~link" and bool(url.strip())
    if argument and not linking:
        return bot.say(functionHelp(when))

    api = bot.getModule("whenapi")
    if not linking:
        row = bot.dbQuery(
            "SELECT url FROM when_links WHERE network = ? AND channel = ?",
            key,
            fetchone,
        )
        if row is None:
            return bot.say(
                "No linked schedule for this channel. Use \x02~link <URL>\x02 to link a schedule"
            )
        url = row["url"]
    try:
        link = api.parse_link(bot, url.strip())
    except ValueError as error:
        return bot.say(str(error))
    try:
        lookahead = bot.getOption("lookahead_hours", module="when")
        if not isfinite(lookahead) or not 0 <= lookahead <= api.MAX_LOOKAHEAD_HOURS:
            return bot.say("Set when.lookahead_hours to a number between 0 and 168.")
        result = api.get_availability(bot, link, lookahead_hours=lookahead)
    except HTTPError:
        log.exception("When schedule query failed")
        return bot.say("When is unavailable; try again later. %s" % link.url)
    if result is None:
        return bot.say(
            "Schedule not found. Use \x02~link <URL>\x02 to link another schedule. %s"
            % link.url
        )
    if linking:
        bot.dbQuery(
            "INSERT INTO when_links (network, channel, url) VALUES (?, ?, ?) "
            "ON CONFLICT(network, channel) DO UPDATE SET url = excluded.url",
            (*key, link.url),
        )
        return bot.say("Linked schedule for this channel: %s" % link.url)
    if not result.available:
        return bot.say(
            "Nobody is available now or in the next %s. %s"
            % (_duration(lookahead), link.url)
        )
    items = []
    for window in result.available:
        duration = ("at least " if window.duration_is_lower_bound else "") + _duration(
            window.duration_hours
        )
        timing = "for " + duration
        if window.starts_in_hours > 0:
            timing = "in %s %s" % (_duration(window.starts_in_hours), timing)
        items.append("%s (%s)" % (_name(window.name), timing))
    return pastehelper(
        bot,
        "\x02Availability:\x02 %s — " + link.url,
        items=items,
        title="When availability",
    )


def init(bot: BotLike) -> bool:
    bot.dbCheckCreateTable(
        "when_links",
        """CREATE TABLE when_links (
            network TEXT NOT NULL,
            channel TEXT NOT NULL,
            url TEXT NOT NULL,
            PRIMARY KEY (network, channel)
        )""",
    )
    return True


mappings = (Mapping(command="when", function=when),)
