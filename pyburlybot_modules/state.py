from typing import Any

from util.threads import call_in_reactor

from util import Mapping, argumentSplit, english_list
from util.event import Event
from util.types import BotLike


def _snapshot(bot: BotLike) -> dict[str, Any]:
    return call_in_reactor(bot.state.snapshot)


def state_command(event: Event, bot: BotLike) -> None:
    """state [channels|channel [name]|user nick]. Inspect tracked IRC state."""
    action, argument = argumentSplit(event.argument, 2)
    action = (action or "network").casefold()
    state = _snapshot(bot)
    channels = state["channels"]
    users = state["users"]

    if action == "network":
        bot.say(
            "Tracking %d users across %d channels on %s."
            % (len(users), len(channels), state["name"])
        )
    elif action == "channels":
        bot.say(
            "Channels: %s" % (english_list(sorted(channels)) if channels else "none")
        )
    elif action == "channel":
        if not argument:
            return bot.say("Usage: state channel #channel")
        channel = channels.get(argument)
        if channel is None:
            return bot.say("No tracked channel named %s." % argument)
        modes = ", ".join(channel["modes"]) or "none"
        bot.say(
            "%s: %d users, %d ops, %d voices; modes: %s; topic: %s"
            % (
                argument,
                len(channel["users"]),
                len(channel["ops"]),
                len(channel["voices"]),
                modes,
                channel["topic"] or "none",
            )
        )
    elif action == "user":
        if not argument:
            return bot.say("Usage: state user nickname")
        user = users.get(argument)
        if user is None:
            return bot.say("No tracked user named %s." % argument)
        bot.say(
            "%s is on %s (%s)."
            % (
                argument,
                english_list(user["channels"]) if user["channels"] else "no channels",
                user["hostmask"] or "hostmask unknown",
            )
        )
    else:
        bot.say("State actions: network, channels, channel, user")


def state_bans(event: Event, bot: BotLike) -> None:
    """statebans [channel]. Show tracked ban masks (administrator only)."""
    state = _snapshot(bot)
    if event.argument:
        selected = {event.argument: state["channels"].get(event.argument)}
    else:
        selected = state["channels"]
    for name, channel in selected.items():
        if channel is None:
            bot.say("No tracked channel named %s." % name)
            continue
        masks = channel["bans"]
        bot.say("Bans on %s: %s" % (name, english_list(masks) if masks else "none"))


def init(bot: BotLike) -> bool:
    return bool(bot.getOption("enablestate"))


mappings = (
    Mapping(command="state", function=state_command),
    Mapping(command="statebans", function=state_bans, admin=True),
)
