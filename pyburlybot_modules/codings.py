from util.event import Event
from util.types import BotLike
from typing import Any, cast

# codings module
# for things like md5ing, rot13, urlencode, unquoting and so on
from hashlib import algorithms_available, md5, new
from zlib import crc32
from urllib.parse import unquote, quote
from codecs import decode as codecs_decode, encode as codecs_encode

from util import Mapping, argumentSplit, functionHelp


def hash(event: Event, bot: BotLike) -> None:
    """hash method content. content will be hashed according to method (after encoding to UTF-8.) Use "hash methods" to see what methods are supported."""
    method, content = argumentSplit(event.argument, 2)
    if not (method and content):
        if method == "methods":
            return bot.say(
                "Supported hash methods: %s" % ", ".join(sorted(algorithms_available))
            )
        return bot.say(functionHelp(hash))
    if method not in algorithms_available:
        return bot.say(
            "Unknown method (%s). Use \x02hash methods\x02 to see what methods are supported."
        )
    h = new(method)
    h.update(content.encode("utf-8"))
    bot.say("%s - %s" % (h.hexdigest(), repr(content)))


def hash_md5(event: Event, bot: BotLike) -> None:
    """md5 content. content will be md5 hashed (after encoding to UTF-8.)"""
    arg = event.argument
    if not arg:
        return bot.say(functionHelp(md5))
    digest = md5(arg.encode("utf-8"), usedforsecurity=False).hexdigest()
    bot.say("%s - %s" % (digest, repr(arg)))


def rot13(event: Event, bot: BotLike) -> None:
    """rot13 content. content will be rot13 encoded."""
    arg = event.argument
    if not arg:
        return bot.say(functionHelp(rot13))
    bot.say(codecs_encode(arg, "rot_13", "ignore"))


def crc(event: Event, bot: BotLike) -> None:
    """crc content. content will be crc32 encoded (after encoding to utf-8.)"""
    arg = event.argument
    if not arg:
        return bot.say(functionHelp(crc))
    bot.say("%x - %s" % (crc32(arg.encode("utf-8")) & 0xFFFFFFFF, repr(arg)))


def funquote(event: Event, bot: BotLike) -> None:
    """unquote content. content will be URL decoded."""
    arg = event.argument
    if not arg:
        return bot.say(functionHelp(funquote))
    bot.say(unquote(str(arg)))


def fquote(event: Event, bot: BotLike) -> None:
    """quote content. content will be URL encoded."""
    arg = event.argument
    if not arg:
        return bot.say(functionHelp(fquote))
    bot.say(quote(arg))


def fencode(event: Event, bot: BotLike) -> None:
    """encode encoding content. content will be encoded according to provided encoding. Will be displayed using python's repr.
    Available encodings: https://docs.python.org/2/library/codecs.html#standard-encodings
    """
    method, content = argumentSplit(event.argument, 2)
    if not (method and content):
        return bot.say(functionHelp(fencode))
    try:
        try:
            bot.say(repr(content.encode(method)))
        except LookupError, UnicodeError:
            bot.say(repr(codecs_encode(content.encode("utf-8"), cast(Any, method))))
    except LookupError:
        bot.say(
            "Unknown encoding. Available encodings: https://docs.python.org/2/library/codecs.html#standard-encodings"
        )
    except UnicodeEncodeError, UnicodeDecodeError:
        bot.say("Can't encode.")


def fdecode(event: Event, bot: BotLike) -> None:
    """decode encoding content. content will be decoded according to provided encoding. Will be displayed using python's repr if not unicode.
    Available encodings: https://docs.python.org/2/library/codecs.html#standard-encodings .
    Append |repr to the method if you are supplying escaped ascii.
    """
    method, content = argumentSplit(event.argument, 2)
    if not (method and content):
        return bot.say(functionHelp(fdecode))
    try:
        if method.endswith("|repr"):
            method = method[:-5]
            escaped = codecs_decode(content, "unicode_escape")
            raw = escaped.encode("latin-1")
        else:
            raw = content.encode("latin-1")
        o = raw.decode(method)
        bot.say(o)
    except LookupError:
        bot.say(
            "Unknown encoding. Available encodings: https://docs.python.org/2/library/codecs.html#standard-encodings"
        )
    except UnicodeEncodeError, UnicodeDecodeError:
        bot.say("Can't decode.")


# mappings to methods
mappings = (
    Mapping(command="hash", function=hash),
    Mapping(command="md5", function=hash_md5),
    Mapping(command="rot13", function=rot13),
    Mapping(command="crc", function=crc),
    Mapping(command="unquote", function=funquote),
    Mapping(command="quote", function=fquote),
    Mapping(command="encode", function=fencode),
    Mapping(command="decode", function=fdecode),
)
