"""Reactor-thread helpers shared by the bot API facades.

Module handlers run in worker threads while settings, the dispatcher and the
IRC protocol are owned by the reactor thread. ``call_in_reactor`` lets one API
serve both sides: it runs ``f`` directly when already in the reactor thread and
hops (blocking) into it otherwise.

Note: ``isInIOThread`` only becomes true once the reactor thread has been
registered. Module ``init()`` runs before ``reactor.run()``, so the entry point
calls ``registerAsIOThread()`` up front (the reactor re-registers the same
thread when it starts).
"""

from collections.abc import Callable
from typing import Any

from twisted.internet import reactor as _reactor
from twisted.internet.threads import blockingCallFromThread
from twisted.python.threadable import isInIOThread, registerAsIOThread

reactor: Any = _reactor

__all__ = ["call_in_reactor", "isInIOThread", "registerAsIOThread"]


def call_in_reactor(f: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``f`` in the reactor thread and return its result, from any thread."""
    if isInIOThread():
        return f(*args, **kwargs)
    return blockingCallFromThread(reactor, f, *args, **kwargs)
