from util.event import Event
from util.types import BotLike
# reload module

from collections import deque
from threading import Lock

from util import Mapping

### Modules should not import this! Unless they have a very good reason to.
from util.settings import ConfigException, Settings
from util.threads import call_in_reactor

### This is only something that modules that know what they are doing should do:
###

# a broadcast "reload" event is delivered once per server; remember handled
# event_ids so the (process-wide) reload only runs once per post
_seen_lock = Lock()
_seen_events: deque[str] = deque(maxlen=64)


def _reallyReload() -> None:
    Settings.reloadStage1()
    Settings.reloadStage2()


def admin_reload_bot(event: Event, bot: BotLike) -> None:
    # reload settings, important to do only from within reactor
    # also refresh dispatchers
    call_in_reactor(_reallyReload)
    # may never get sent if bot is disconnecting from this server after reload
    return bot.say("Done.")


def reload_event(event: Event, bot: BotLike) -> None:
    """Handle a posted "reload" event (e.g. from the webhook module).

    Only authorized events (event.authorized, i.e. the poster proved it acts for
    the bot owner) trigger a reload of the config file and modules.
    """
    source = getattr(event, "remote", None) or "internal"
    if not event.authorized:
        print("RELOAD: ignoring unauthorized reload event from %s" % source)
        return
    event_id = getattr(event, "event_id", None)
    if event_id is not None:
        with _seen_lock:
            if event_id in _seen_events:
                return
            _seen_events.append(event_id)
    print("RELOAD: reloading configuration (event from %s)" % source)
    try:
        call_in_reactor(_reallyReload)
    except ConfigException as e:
        # nobody to reply to: make the failure obvious in the log
        print("RELOAD: configuration NOT reloaded, config file error: %s" % e)
        raise


mappings = (
    Mapping(command="reload", function=admin_reload_bot, admin=True),
    Mapping(types=["reload"], function=reload_event),
)
