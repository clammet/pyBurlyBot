from collections import deque
from functools import partial
from threading import Lock
from time import monotonic

from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread

from util import Mapping, argumentSplit, functionHelp, pastehelper
from util.event import Event
from util.settings import Settings
from util.types import BotLike


OPTIONS = {
    "commands": (
        list,
        "List of [[command aliases], output] entries.",
        [[["hello"], "world."]],
    ),
    "mutation_limit": (
        int,
        "Maximum edits per identity during a rate window; 0 disables.",
        5,
    ),
    "mutation_window": (int, "Rate-limit window in seconds.", 60),
}

MANAGEMENT_COMMANDS = ("simplecommands", "simplecommand", "sc")
_rate_events: dict[tuple[str, str], deque[float]] = {}
_rate_lock = Lock()


def _rate_limit(event: Event, bot: BotLike) -> bool:
    limit = bot.getOption("mutation_limit", module="simplecommands")
    window = bot.getOption("mutation_window", module="simplecommands")
    if limit <= 0:
        return True
    if window <= 0:
        raise ValueError("simplecommands mutation_window must be positive")
    identity = event.account or event.hostmask or event.nick or "unknown"
    key = (bot.network, identity.casefold())
    now = monotonic()
    with _rate_lock:
        events = _rate_events.setdefault(key, deque())
        while events and events[0] <= now - window:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
    return True


def _save_and_reload(bot: BotLike) -> None:
    Settings.saveOptions()
    bot.reloadModules(inreactor=True)


def _aliases(entry: object) -> list[str]:
    if not isinstance(entry, (list, tuple)) or len(entry) != 2:
        return []
    aliases = entry[0]
    if isinstance(aliases, str):
        return [aliases]
    if isinstance(aliases, (list, tuple)) and all(
        isinstance(item, str) for item in aliases
    ):
        return list(aliases)
    return []


def _find_matches(commands: list, aliases: list[str]) -> list:
    requested = {alias.casefold() for alias in aliases}
    return [
        entry
        for entry in commands
        if requested.intersection(alias.casefold() for alias in _aliases(entry))
    ]


def simplecommands(event: Event, bot: BotLike) -> None:
    """simplecommands [~del name|~list|name[,alias] output]. Manage text commands."""
    first, second = argumentSplit(event.argument, 2)
    commands = bot.getOption("commands", module="simplecommands")
    if not first:
        return bot.say(functionHelp(simplecommands))
    if first == "~list":
        command_names = [
            "(%s)" % ", ".join(aliases)
            for entry in commands
            if (aliases := _aliases(entry))
        ]
        return pastehelper(
            bot,
            "Simplecommands: %s",
            items=sorted(command_names),
            title="Simplecommands",
        )

    deleting = first == "~del" and bool(second)
    mutating = deleting or bool(second)
    if mutating and not _rate_limit(event, bot):
        return bot.say("Simplecommand edit rate limit reached; try again later.")

    if deleting and second is not None:
        aliases = [item.strip() for item in second.split(",") if item.strip()]
        matches = _find_matches(commands, aliases)
        if not matches:
            return bot.say("(%s) is not a known simplecommand." % second)
        if len(matches) > 1:
            return bot.say(
                "That name matches more than one simplecommand; use an unambiguous alias."
            )
        commands.remove(matches[0])
        bot.setOption("commands", commands, module="simplecommands", channel=False)
        blockingCallFromThread(reactor, _save_and_reload, bot)
        return bot.say("Simplecommand (%s) deleted and configuration saved." % second)

    if second:
        aliases = [item.strip() for item in first.split(",") if item.strip()]
        if not aliases:
            return bot.say("At least one non-empty command name is required.")
        if any(any(character.isspace() for character in alias) for alias in aliases):
            return bot.say("Simplecommand names cannot contain whitespace.")
        matches = _find_matches(commands, aliases)
        if len(matches) > 1:
            return bot.say("Those aliases match more than one existing simplecommand.")
        if matches:
            commands.remove(matches[0])
        else:
            for alias in aliases:
                existing = bot.getCommandMappings(alias.casefold())
                if existing:
                    return bot.say(
                        "Command (%s) is already in use by the %s module."
                        % (alias, existing[0].function.__module__)
                    )
        commands.append([aliases, second])
        bot.setOption("commands", commands, module="simplecommands", channel=False)
        blockingCallFromThread(reactor, _save_and_reload, bot)
        action = "replaced" if matches else "added"
        return bot.say(
            "Simplecommand (%s) %s and configuration saved." % (first, action)
        )

    matches = _find_matches(commands, [first])
    if not matches:
        return bot.say("(%s) is not a known simplecommand." % first)
    aliases = _aliases(matches[0])
    bot.say("Simplecommand (%s): %s" % (", ".join(aliases), matches[0][1]))


def echo_this(text: str, event: Event, bot: BotLike) -> None:
    bot.say(text)


def get_mappings(bot: BotLike) -> tuple[Mapping, ...]:
    dynamic = []
    for entry in bot.getOption("commands", module="simplecommands"):
        aliases = _aliases(entry)
        if aliases and isinstance(entry[1], str):
            dynamic.append(
                Mapping(
                    command=aliases, function=partial(echo_this, entry[1]), hidden=True
                )
            )
    return (*mappings, *dynamic)


def init(bot: BotLike) -> bool:
    return True


mappings = (
    Mapping(command=("simplecommands", "simplecommand", "sc"), function=simplecommands),
)
