from re import IGNORECASE, compile as compile_re
from collections.abc import Iterator
from unicodedata import lookup, name

from util import Mapping, functionHelp
from util.event import Event
from util.types import BotLike


RPLFORMAT = "U+%04X %s (%s)"
REGHEX = compile_re(r"^(?:U\+)?([0-9A-F]{4,6})$", IGNORECASE)
MAX_CODEPOINT = 0x10FFFF
MAX_SEARCH_RESULTS = 9


def _getname(character: str) -> str:
    try:
        return name(character)
    except ValueError:
        return "NO NAME"


def _search_names(query: str) -> Iterator[tuple[int, str]]:
    """Lazily scan Unicode names and stop once callers have enough matches."""
    normalized = " ".join(query.upper().split())
    try:
        exact = lookup(normalized)
    except KeyError:
        exact = None
    if exact is not None:
        yield ord(exact), name(exact)
    exact_ordinal = ord(exact) if exact is not None else None
    for ordinal in range(MAX_CODEPOINT + 1):
        if ordinal == exact_ordinal:
            continue
        try:
            description = name(chr(ordinal))
        except ValueError:
            continue
        if normalized in description:
            yield ordinal, description


def funicode(event: Event, bot: BotLike) -> None:
    """unicode [characters|description|4-6 digit hex]. Show Unicode information."""
    argument = event.argument
    if not argument:
        return bot.say(functionHelp(funicode))
    hex_match = REGHEX.fullmatch(argument)
    if hex_match:
        ordinal = int(hex_match.group(1), 16)
        if ordinal > MAX_CODEPOINT:
            return bot.say("Code point is outside Unicode's range.")
        character = chr(ordinal)
        return bot.say(RPLFORMAT % (ordinal, _getname(character), character))
    if len(argument) <= 3:
        return bot.say(
            ", ".join(
                RPLFORMAT % (ord(character), _getname(character), character)
                for character in argument
            )
        )

    output = []
    for ordinal, description in _search_names(argument):
        output.append(RPLFORMAT % (ordinal, description, chr(ordinal)))
        if len(output) == MAX_SEARCH_RESULTS:
            break
    bot.say(", ".join(output) if output else "No characters found.")


mappings = (Mapping(command=("u", "unicode"), function=funicode),)
