from collections.abc import Callable, Iterable
from time import struct_time
from typing import Any, TextIO, cast

# timehelpers.py
from datetime import UTC, timedelta, datetime
from time import time
from calendar import day_abbr, day_name, timegm
from codecs import lookup
from operator import itemgetter
from shlex import shlex
from inspect import getdoc
import re
from fnmatch import fnmatchcase

WDAY_MAP = dict(enumerate(day_name))
WDAY_SHORTMAP = dict(enumerate(day_abbr))


# synodic lunar month, the same constant TIMEREGEX parsing uses, so
# "in 3months" round-trips to "in 3 months"
MONTH_SECS = 60 * 60 * 24 * 29.53059
WEEK_SECS = 60 * 60 * 24 * 7


# adapted http://stackoverflow.com/a/2119512
def days_hours_minutes(td: timedelta) -> tuple[int, int, int, int]:
    return td.days, td.seconds // 3600, (td.seconds // 60) % 60, td.seconds % 60


def pluralize(term: str, num: int | float) -> str:
    if num > 1:
        return term + "s"
    else:
        return term


# distance_of_time_in_words hardcoded granularity
def distance_of_time_in_words(
    fromtime: int | float, totime: int | float | None = None, suffix: str = "ago"
) -> str:
    if not totime:
        totime = time()
    past = True
    diff = totime - fromtime
    if diff < 0:
        past = False
        diff = abs(diff)
    if diff < 20:
        if past:
            return "just a moment %s" % suffix
        else:
            return "in just a moment"

    months = int(diff // MONTH_SECS)
    diff -= months * MONTH_SECS
    weeks = int(diff // WEEK_SECS)
    diff -= weeks * WEEK_SECS
    td = timedelta(seconds=diff)
    days, hours, minutes, seconds = days_hours_minutes(td)

    chunks = []
    terms: tuple[tuple[str, int], ...]
    if months or weeks or days or hours or minutes > 10:
        terms = (
            ("month", months),
            ("week", weeks),
            ("day", days),
            ("hour", hours),
            ("minute", minutes),
        )
    else:
        terms = (
            ("day", days),
            ("hour", hours),
            ("minute", minutes),
            ("second", seconds),
        )
    for term, value in terms:
        if value:
            chunks.append((value, pluralize(term, value)))

    s = ""
    while chunks:
        s += "%s %s" % chunks.pop(0)
        if len(chunks) >= 2:
            s += ", "
        elif len(chunks) == 1:
            s += " and "
        else:
            if past:
                s += " %s" % suffix
            else:
                s = "in " + s
    return s


# isIterable (the tuple or list kind of iterable)
# maybe there is a more apt name
def isIterable(i: object) -> bool:
    return isinstance(i, (set, list, tuple))


def processHostmask(h: str | None) -> tuple[str | None, str | None, str | None]:
    if h:
        try:
            nick, ident = h.split("!", 1)
            ident, host = ident.split("@", 1)
        except ValueError:
            pass
        else:
            return (nick, ident, host)
    return (None, None, None)


# Useful thing http://stackoverflow.com/a/8528866
# This may return incorrectly decoded string because naive
ENCODINGS = (
    "utf-8",
    "sjis",
    # latin_1 decodes any byte sequence, so it acts as the catch-all;
    # codecs listed after it would never be tried
    "latin_1",
)


def coerceToUnicode(s: Any, enc: str | None = None) -> str:
    if isinstance(s, str):
        return s
    if not isinstance(s, bytes):
        return str(s)
    if enc:
        try:
            return s.decode(enc)
        except UnicodeDecodeError:
            pass
    for fallback_enc in ENCODINGS:
        try:
            return s.decode(fallback_enc)
        except UnicodeDecodeError:
            continue
    # unreachable while latin_1 is in ENCODINGS; kept as a safety net
    return s.decode("utf-8", "replace")


def processListReply(
    params: list[str] | tuple[str, ...],
) -> tuple[str, str, str | None, str | None, str | None, str, str]:
    channel = params[1]
    mask = params[2]
    nick, ident, host = processHostmask(params[3])
    t = params[4]
    return channel, mask, nick, ident, host, t, params[3]


# TODO: This seems pretty clunky. Maybe revisit/refactor it in future...
class PrefixMap:
    def __init__(self, prefixiter: Iterable[tuple[str, tuple[str, int]]]) -> None:
        self.loadfromprefix(prefixiter)

    def loadfromprefix(self, prefixiter: Iterable[tuple[str, tuple[str, int]]]) -> None:
        prefixes = []
        opfixes = []
        opcmds = []
        foundop = False
        foundvoice = False
        usermodemap = {}
        voicefixes = []
        voicecmds = []
        for cmd, p, _ in sorted(
            ((cmd, p, num) for cmd, (p, num) in prefixiter), key=itemgetter(2)
        ):
            # ('~', 0)
            # identify index of traditional op (@) and class everything under "op"
            # also identify index of voice and likewise
            prefixes.append(p)
            if not foundop:
                opfixes.append(p)
                opcmds.append(cmd)
            elif not foundvoice:
                voicefixes.append(p)
                voicecmds.append(cmd)
            if p == "@":
                foundop = True
            elif p == "+":
                foundvoice = True
            usermodemap[cmd] = p

        self.opprefixes = "".join(opfixes)
        self.opcmds = "".join(opcmds)
        self.nickprefixes = "".join(prefixes)
        self.usermodemap = usermodemap
        self.voiceprefixes = "".join(voicefixes)
        self.voicecmds = "".join(voicecmds)


# Simple command parse and return (command, argument)
# split arguments in to [nargs] number of elements in the case of nargs > 1 else argument will be singular string
# if pad=False: if len(arguments) < nargs return None as argument,
# else pad missing arguments with None
def commandSplit(
    s: str | None, nargs: int = 1, pad: bool = True
) -> tuple[str | None, Any]:
    if s:
        parts = s.split(None, 1)
        if len(parts) > 1:
            if nargs > 1:
                a = argumentSplit(parts[1], nargs, pad)
                if a:
                    return (parts[0], a)
                else:
                    return (parts[0], None)
            else:
                return parts[0], parts[1]
        else:
            if pad and nargs > 1:
                return parts[0], (None,) * nargs
            return parts[0], None
    return (None, None)


# like commandSplit, this is only for splitting arguments up
def argumentSplit(s: str | None, nargs: int, pad: bool = True) -> list[str | None]:
    """Splits provided s in to a list of arguments up to nargs. If pad is true, it will pad
    the remaining args up to nargs with None.
    """
    if s:
        lexer = shlex(s, posix=False)
        lexer.commenters = ""
        lexer.whitespace_split = True
        i = 0
        args: list[str | None] = []
        while (i < nargs - 1) or nargs == -1:  # allows to split entire string
            tok = lexer.get_token()
            if tok and len(tok) >= 2 and tok[0] in lexer.quotes and tok[-1] == tok[0]:
                tok = tok[1:-1]
            if not tok:
                break
            args.append(tok)
            i += 1
        rest = (
            cast(TextIO, lexer.instream).read().strip()
        )  # TODO: should this really be stripping here? Without strip:
        if rest:  # >>> argumentSplit('one "two three" four', 3)
            args.append(rest)  # ['one', 'two three', ' four']
            i += 1
        if pad:
            while i < nargs:
                args.append(None)
                i += 1
        return args
    else:
        if pad:
            return [None] * nargs
        else:
            return []


# TODO: add more outgoing things here for length calculation
commandlength = {
    "sendmsg": "PRIVMSG %s :",
}


# Complicated method. Will split a unicode string to desires length without returning
# malformed unicode strings.
# Will return a list of (stringsegment, length of encoding) tuples.
def splitEncodedUnicode(
    s: str, length: int, encoding: str = "utf-8", n: int = 1
) -> list[tuple[str, int]]:
    if length < 1:
        return [("", 0)]
    es = s.encode(encoding)
    le = len(es)
    if le <= length:
        return [(s, le)]
    else:
        splits: list[tuple[str, int]] = []
        ib = 0  # start of segment
        # UTF-8 makes this somewhat easy
        if lookup(encoding).name == "utf-8":
            while ib < le and len(splits) < n:
                ie = ib + length  # end of segment
                if ie >= le:
                    segment = es[ib:ie]
                    splits.append((segment.decode("utf-8"), len(segment)))
                    break
                c = es[ie]
                # check for unicode character start byte, and backtrack if not found
                while (
                    (0b10000000 & c != 0) and (0b11000000 & c != 0b11000000) and ie > ib
                ):
                    ie -= 1
                    c = es[ie]
                splits.append((es[ib:ie].decode("utf-8"), ie - ib))
                if ib == ie:
                    # in rare case that a character can't fit, skip it.
                    ie += 1
                    try:
                        c = es[ie]
                        while (0b10000000 & c != 0) and (0b11000000 & c != 0b11000000):
                            ie += 1
                            c = es[ie]
                    except IndexError:
                        break  # break if end of encoded string is reached.
                ib = ie
            # it might be faster to calc all the endchar points first and then translate back.
        else:
            # not as bad as I thought it would be, but pretty bad
            sl = len(s)  # length of original string
            while ib < sl and len(splits) < n:
                ie = ib + length  # end of segment
                ss = s[ib:ie]  # original string spliced
                sse = ss.encode(encoding)  # encoding of that splice
                le = len(sse)  # length of encoded splice
                while le > length:
                    ie -= round(
                        (le - length) / 1.8
                    )  # trim 1.8 times the extra length, seemed like good compromise
                    ss = s[ib:ie]
                    sse = ss.encode(encoding)
                    le = len(sse)
                splits.append((ss, le))
                ib = ie
        return splits


# retrieve help for function f. sub will provide help for that sub command
def functionHelp(f: Callable[..., Any], sub: str | None = None) -> str:
    doc = getdoc(f)
    if doc:
        docs = doc.replace("\n", " ").split("|")
        if not sub:
            return docs[0]
        else:
            for subdoc in docs:
                try:
                    if subdoc.split(" ", 1)[1].startswith(sub):
                        return subdoc
                except IndexError:
                    pass
            return docs[0]
    else:
        return ""


# this is getting a bit out of hand...
# TODO: check if this is very bad.
# \s* allows spaced specs ("3 days", "1h 30m") as well as the packed originals
TIMEREGEX = re.compile(
    r"""
(?:(\d*\.?\d+)\s*months?\s*)?
(?:(\d*\.?\d+)\s*w(?:eeks?)?\s*)?
(?:(\d*\.?\d+)\s*d(?:ays?)?\s*)?
(?:(\d*\.?\d+)\s*h(?:ours?)?\s*)?
(?:(\d*\.?\d+)\s*m(?:in(?:s|utes?)?)?\s*)?
(?:(\d*\.?\d+)\s*s(?:ec(?:s|onds?)?)?)?
""",
    re.VERBOSE | re.IGNORECASE,
)


def _parseDigit(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return 0


def parseDateTime(
    s: str, t: int | float | struct_time | tuple[int, ...] | None = None
) -> int | float | None:
    if not t:
        t = time()
    elif not (isinstance(t, float) or isinstance(t, int)):
        t = timegm(t)
    s = s.strip().lower()
    # even though "at 2/2 sounds odd, allow it so that all the 'absolute relative' timecodes are in one place
    if s.startswith("on") or s.startswith("at"):
        # absolute relative (lol) date. e.g. 5/3, 2014/06/31, etc also Monday, Tuesday, etc
        dd = datetime.fromtimestamp(t, UTC).replace(tzinfo=None)
        s = s[2:].strip()
        pd = None
        for index, dformat in enumerate(
            ("%Y/%m/%d", "%m/%d", "%dth", "%dst", "%dnd", "%drd", "%H:%M", "%I%p")
        ):
            try:
                pd = datetime.strptime(s, dformat)  # noqa: DTZ007 - partial date, combined below
            except ValueError:
                continue
            # add year
            if index != 0:
                pd = pd.replace(year=dd.year)
                if index == 1:
                    if (dd.month == pd.month) and (dd.day >= pd.day):
                        pd = pd.replace(year=dd.year + 1)
                    elif dd.month > pd.month:
                        pd = pd.replace(year=dd.year + 1)
            # add month
            if index >= 2:
                # add month until find month where provided day fits. (Needed for things like "on 30th" if Feb)
                count = 0
                month = dd.month
                while True:
                    if count > 10:
                        return None  # Bail in the odd event that we can't find a month another 10 attempts.
                    try:  # Don't think this will ever happen though. Should only ever attempt 2
                        pd = pd.replace(month=month)
                        break
                    except ValueError:
                        month += 1
                        count += 1
                        continue
                if (index < 6) and (dd.day >= pd.day):
                    # day already passed this month: advance to the next month
                    # that can hold the provided day (may wrap into next year)
                    month += 1
                    count = 0
                    while True:
                        if count > 10:
                            return None
                        try:
                            pd = pd.replace(
                                month=(month - 1) % 12 + 1,
                                year=dd.year + (month - 1) // 12,
                            )
                            break
                        except ValueError:
                            month += 1
                            count += 1
            # add day
            if index >= 6:
                pd = pd.replace(day=dd.day)
                if (dd.hour, dd.minute) >= (pd.hour, pd.minute):
                    pd += timedelta(days=1)
            break
        else:
            # check Mon(day), Tues(day), etc
            days = 0
            for index, check in enumerate(
                (
                    ("m", "mon", "monday"),
                    ("t", "tue", "tues", "tuesday"),
                    ("w", "wed", "wednesday"),
                    ("th", "thurs", "thursday"),
                    ("f", "fri", "friday"),
                    ("s", "sat", "saturday"),
                    ("su", "sun", "sunday"),
                )
            ):
                if s in check:
                    wd = dd.weekday()
                    if index <= wd:
                        days = index + (7 - wd)
                    else:
                        days = index - wd
                    break
            else:
                # finally check for lunch
                if s == "lunch":
                    if dd.hour >= 12:
                        dd += timedelta(days=1)
                    return timegm(dd.replace(hour=12, minute=0, second=0).timetuple())
                return None
            pd = (dd + timedelta(days=days)).replace(hour=0, minute=0, second=0)
        return timegm(pd.timetuple())

    if s == "tomorrow":
        # special case similar to above
        dd = datetime.fromtimestamp(t, UTC).replace(tzinfo=None)
        if dd.hour >= 5:
            dd += timedelta(days=1)
        return timegm(dd.replace(hour=7, minute=0, second=0).timetuple())

    if s.startswith("in"):
        # relative time. e.g. 5minutes, 10hours, 3days
        s = s[2:].strip()
    # if no marker assume relative time
    m = TIMEREGEX.match(s)
    if m and (
        m.group(1) or m.group(2) or m.group(3) or m.group(4) or m.group(5) or m.group(6)
    ):
        if m.group(1):
            # months (just to be silly, a synodic lunar month)
            t += _parseDigit(m.group(1)) * MONTH_SECS
        if m.group(2):
            # weeks
            t += _parseDigit(m.group(2)) * WEEK_SECS
        if m.group(3):
            # days
            t += _parseDigit(m.group(3)) * 60 * 60 * 24
        if m.group(4):
            # hours
            t += _parseDigit(m.group(4)) * 60 * 60
        if m.group(5):
            # mins
            t += _parseDigit(m.group(5)) * 60
        if m.group(6):
            # secs
            t += _parseDigit(m.group(6))
        return t
    return None


def irc_casefold(value: str, casemapping: str = "rfc1459") -> str:
    """Apply an IRC casemapping instead of locale/Unicode case conversion."""

    value = value.lower()
    if casemapping == "ascii":
        return value
    if casemapping == "strict-rfc1459":
        return value.translate(str.maketrans("[]\\", "{}|"))
    if casemapping == "rfc1459":
        return value.translate(str.maketrans("[]\\^", "{}|~"))
    raise ValueError("Unknown IRC casemapping: %s" % casemapping)


def match_hostmask(s: str, mask: str, casemapping: str = "rfc1459") -> bool:
    """Match a complete IRC mask with shell wildcards and IRC case rules."""

    return fnmatchcase(irc_casefold(s, casemapping), irc_casefold(mask, casemapping))
