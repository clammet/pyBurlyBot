from util.event import Event
from util.types import BotLike, DatabaseQuery
# urlinfo module

from util import Mapping, fetchone, URLREGEX
from util.http import HTTPError, HTTPClient
from re import compile as recompile, IGNORECASE, DOTALL

from html import unescape

# (code - reason) content-type, encoding, size, serversoftware, redirect
HEAD_RPL = "(%s - %s) %s, %s%s bytes, %s%s"
TITLE_REGEX = recompile("<title>(.*?)</title>", IGNORECASE | DOTALL)
METADATA_HTTP = HTTPClient(max_bytes=64 * 1024)


def seen_link(event: Event, bot: BotLike) -> None:
    match = event.regex_match
    pos = match.regs[0]
    url = match.string[pos[0] : pos[1]]
    bot.dbQuery(
        """INSERT OR REPLACE INTO urlinfo (source, url)
        VALUES (?,?);""",
        (event.target, url),
    )


def _getURL(event: Event, dbQuery: DatabaseQuery) -> str | None:
    row = dbQuery(
        """SELECT url FROM urlinfo
                            WHERE source=?;""",
        (event.target,),
        fetchone,
    )
    if not row:
        return None
    return row["url"]


def lasturl(event: Event, bot: BotLike) -> None:
    url = _getURL(event, bot.dbQuery)
    if not url:
        return bot.say("Haven't seen any URLs in here.")
    bot.say(url)


def headers(event: Event, bot: BotLike) -> None:
    """head [URL]. If no argument is provided the headers of the last URL will be displayed.
    Otherwise the title of the provided URL will be displayed."""
    if not event.argument:
        url = _getURL(event, bot.dbQuery)
        if not url:
            return bot.say("Haven't seen any URLs in here.")
    else:
        url = event.argument
        if not url.startswith("http"):
            url = "http://" + url

    try:
        resp = METADATA_HTTP.head(url)
    except (HTTPError, TimeoutError) as exc:
        return bot.say("Couldn't retrieve headers: %s" % exc)
    h = resp.headers
    ctype = h.get("content-type", "?;").split(";")[0]
    server = h.get("server", "?")
    try:
        size = int(h.get("content-length", 0))
    except ValueError:
        size = 0
    location = " -> %s" % resp.url if resp.url != url else ""
    bot.say(HEAD_RPL % (resp.status, resp.reason, ctype, "", size, server, location))


def title(event: Event, bot: BotLike) -> None:
    """title [URL]. If no argument is provided the title of the last URL will be displayed.
    Otherwise the title of the provided URL will be displayed."""
    if not event.argument:
        url = _getURL(event, bot.dbQuery)
        if not url:
            return bot.say("Haven't seen any URLs in here.")
    else:
        url = event.argument
        if not url.startswith("http"):
            url = "http://" + url

    try:
        resp = METADATA_HTTP.get(url)
    except (HTTPError, TimeoutError) as exc:
        return bot.say("Couldn't retrieve title: %s" % exc)
    # only if content-type is html though
    ctype = resp.headers.get("content-type", "?;").split(";")[0]
    if ctype == "text/html":
        m = TITLE_REGEX.search(resp.text)
        if m:
            title = " ".join(unescape(m.group(1)).split())
            bot.say("Title: %s" % title)
        else:
            bot.say("Couldn't find a title in (%s)." % url)
    else:
        # TODO: Maybe display last portion of pathname using something like os.path.basename
        bot.say("No title for (%s) type in (%s)." % (ctype, url))


def init(bot: BotLike) -> bool:
    bot.dbCheckCreateTable(
        "urlinfo",
        """CREATE TABLE urlinfo(
            source TEXT PRIMARY KEY COLLATE NOCASE,
            url TEXT
        );""",
    )
    return True


# mappings to methods
mappings = (
    Mapping(command=("head",), function=headers),
    Mapping(command=("title",), function=title),
    Mapping(command=("lasturl",), function=lasturl),
    Mapping(types=["privmsged"], regex=URLREGEX, function=seen_link),
)
