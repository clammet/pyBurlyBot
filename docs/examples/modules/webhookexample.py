from util.event import Event
from util.types import BotLike
# Posted-event and webhook example; copy into pyburlybot_modules before enabling it.
#
# Demonstrates the two halves of module events:
#   * listening for events posted by other modules (here: the webhook module)
#   * posting your own events for other modules to consume

from util import Mapping

OPTIONS = {
    "announce_channel": (
        str,
        "Channel that receives a line for every webhook the bot accepts.",
        "",
    ),
}


def on_webhook(event: Event, bot: BotLike) -> None:
    """Runs for every HTTP request the webhook module accepted (any hook name).

    Posted events have no reply target, so bot.say() is unavailable here;
    address a channel explicitly instead.
    """
    channel = bot.getOption("announce_channel", module="webhookexample")
    if not channel:
        return
    # event.authorized is True only when the request carried the configured secret
    trust = "authorized" if event.authorized else "anonymous"
    summary = event.json if event.json is not None else event.body[:80]
    bot.sendmsg(
        channel,
        "webhook %s from %s (%s): %r" % (event.hook, event.remote, trust, summary),
    )


def on_deploy(event: Event, bot: BotLike) -> None:
    """Runs only for POST /hooks/deploy, once 'deploy' is listed in the webhook
    module's event_hooks option. Privileged: require an authorized request."""
    if not event.authorized:
        return
    bot.postEvent(
        "announce", text="deploy finished: %s" % (event.json or {}).get("version")
    )


def on_announce(event: Event, bot: BotLike) -> None:
    """A module-posted event; any module may post it with bot.postEvent("announce", text=...)."""
    channel = bot.getOption("announce_channel", module="webhookexample")
    if channel:
        bot.sendmsg(channel, event.text)


def announce(event: Event, bot: BotLike) -> None:
    """announce <text>. Post an "announce" event from an IRC command."""
    if event.argument:
        bot.postEvent("announce", text=event.argument)


mappings = (
    Mapping(types=["webhook"], function=on_webhook),
    Mapping(types=["deploy"], function=on_deploy),
    Mapping(types=["announce"], function=on_announce),
    Mapping(command="announce", function=announce, admin=True),
)
