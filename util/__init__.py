from collections.abc import Sequence
from logging import getLogger
from typing import Any
from re import compile as recompile, IGNORECASE, VERBOSE
from .timer import Timers, TimerExists, TimerInvalidName, TimerNotFound
from .container import TimeoutException
from .helpers import (
    distance_of_time_in_words,
    processHostmask,
    commandSplit,
    argumentSplit,
    commandlength,
    functionHelp,
    coerceToUnicode,
    parseDateTime,
    match_hostmask,
    WDAY_MAP,
    WDAY_SHORTMAP,
)
from .helpers import irc_casefold
from .mapping import Mapping
from .db import fetchone, fetchall, fetchmany
from .types import BotLike, DatabaseParams, DatabaseQuery
from .options import Option

__all__ = (
    "URLREGEX",
    "WDAY_MAP",
    "WDAY_SHORTMAP",
    "BotLike",
    "DatabaseParams",
    "DatabaseQuery",
    "Mapping",
    "Option",
    "TimeoutException",
    "TimerExists",
    "TimerInvalidName",
    "TimerNotFound",
    "Timers",
    "argumentSplit",
    "coerceToUnicode",
    "commandSplit",
    "commandlength",
    "distance_of_time_in_words",
    "english_list",
    "fetchall",
    "fetchmany",
    "fetchone",
    "functionHelp",
    "irc_casefold",
    "match_hostmask",
    "parseDateTime",
    "pastehelper",
    "processHostmask",
)


def pastehelper(
    bot: BotLike,
    basemsg: str,
    items: Sequence[str] | None = None,
    altmsg: str | None = None,
    sep: tuple[str, str] = (", ", "\n"),
    force: bool = False,
    **kwargs: Any,
) -> None:
    """If using items, altmsg is an alternate string to interpolate with the items list."""
    try:
        tmsg = basemsg
        if not force:
            if items is not None:
                tmsg = basemsg % sep[0].join(items)
            if bot.checkSay(tmsg):
                return bot.say(tmsg)
        # guard only the addon lookup, so a genuine AttributeError raised
        # inside the paste addon (or say) is not misreported as "no addon"
        try:
            paste = bot.getAddon("paste")
        except AttributeError:
            if items is not None:
                bot.say(basemsg % "Error: too many entries to list and no paste addon.")
            else:
                bot.say(basemsg % "Error: too much data and no paste addon.")
            return
        if items is not None:
            url = paste((altmsg or basemsg) % sep[1].join(items), bot=bot, **kwargs)
        else:
            url = paste(basemsg, bot=bot, **kwargs)
        if url:
            bot.say(basemsg % url)
        else:
            bot.say(basemsg % "Error: paste addon failure.")
    except Exception:
        # make sure contents of paste is at least dumped somewhere for recovery if need be.
        if items is not None:
            if altmsg:
                tmsg = altmsg % sep[1].join(items)
            else:
                tmsg = basemsg % sep[1].join(items)
        else:
            tmsg = basemsg
        getLogger(__name__).error("ATTEMPTED PASTEHELPER MSG: %r", tmsg)
        raise


def english_list(items: str | Sequence[str]) -> str:
    """Stringify a list into 'arg1, arg2 and arg3', or 'arg1' if single-argument."""
    values = (items,) if isinstance(items, str) else items
    if len(values) > 2:
        return "%s, and %s" % (", ".join(values[:-1]), values[-1])
    elif len(values) == 2:
        return "%s and %s" % (values[0], values[1])
    else:
        return values[0] if values else ""


URLREGEX = recompile(
    r"""
\bhttps?\://                    # schema
[\w.\:-]+                        # domain
(?:/)?                            # first path separator
(?:[\w%./_~!$&'()*+,;=:@-]+)?    # path
(?:\?[^ #\n\r]+)?                # querystring
(?:\#[^ #\n\r]+)?                # anchor (shouldn't be nested in querystring group)
""",
    IGNORECASE | VERBOSE,
)
