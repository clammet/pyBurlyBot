from re import Match
from typing import Any
from twisted.words.protocols.irc import CHANNEL_PREFIXES
from .helpers import coerceToUnicode

from time import time
from datetime import UTC, datetime


# NOTHING IN EVENT SHOULD BE MODIFIED BY MODULES EVER, THANKS.
# TODO: I think prefix and hostmask are always the same. What to do?
class Event:
    regex_match: Match[str]

    def __init__(
        self,
        type: str | None,
        prefix: str | None = None,
        params: list[str] | None = None,
        hostmask: str | None = None,
        target: str | None = None,
        msg: str | None = None,
        nick: str | bytes | None = None,
        ident: str | bytes | None = None,
        host: str | None = None,
        encoding: str = "utf-8",
        command: str | None = None,
        argument: str | None = None,
        account: str | None = None,
        priority: int = 10,
        authorized: bool = False,
        **kwargs: Any,
    ) -> None:
        self.type = type
        self.prefix = prefix
        self.params = params
        self.hostmask = hostmask
        self.nick: str | None = coerceToUnicode(nick, encoding) if nick else None
        self.ident: str | None = coerceToUnicode(ident, encoding) if ident else None
        # Note: if unicode/punycode hostnames becomes a thing for IRC, .decode("idna") I guess
        self.host = host
        self.account = account if account and account != "*" else None

        self.target = coerceToUnicode(target, encoding) if target else target

        # if there is a msg, it's already unicode (done in dispatcher.)
        self.msg = msg

        self.command = command
        self.argument = argument

        # kwargs is a dict of uncommon event attributes which will be looked up on attribute access
        self.kwargs = kwargs

        # True when the event originates from a source the bot owner has
        # authorized (e.g. a webhook request that carried the configured secret).
        # Never set for ordinary IRC traffic. Handlers for privileged actions
        # such as reload must check this before acting.
        self.authorized = bool(authorized)

        # might be useful
        self.time = time()
        self.dtime = datetime.now(UTC)
        self.priority = priority

    def __repr__(self) -> str:
        return (
            "Event(type=%r, prefix=%r, params=%r, hostmask=%r, nick=%r, ident=%r, host=%r, "
            "target=%r, msg=%r, command=%r, argument=%r, authorized=%r, kwargs=%r, time=%r"
            % (
                self.type,
                self.prefix,
                self.params,
                self.hostmask,
                self.nick,
                self.ident,
                self.host,
                self.target,
                self.msg,
                self.command,
                self.argument,
                self.authorized,
                self.kwargs,
                self.time,
            )
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __getattr__(self, name: str) -> Any:
        # only called when normal attribute lookup fails; fall back to kwargs
        # Access __dict__ directly because copy() may probe a newly allocated
        # Event before its attributes (including kwargs) have been restored.
        kwargs = object.__getattribute__(self, "__dict__").get("kwargs", {})
        try:
            return kwargs[name]
        except KeyError:
            raise AttributeError(name) from None

    # TODO: Should this be called "isQuery" ?
    def isPM(self) -> bool:
        return self.target is not None and self.target[0] not in CHANNEL_PREFIXES
