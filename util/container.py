from collections.abc import Callable, Generator, Iterable, Sequence
from types import ModuleType
from typing import Any, NoReturn, TypeAlias
# container...
# This module does a few things. It holds a reference to the current botinstance
# it wraps actual botinst functions to limit the scope of what functions modules have access to within botinstance
# it also holds a queue for messages attempted to be sent while there is no current botinstance

from logging import getLogger
from queue import Queue, Empty
from collections import deque
from time import time, sleep
from functools import partial
from uuid import uuid4

from twisted.internet import reactor as _reactor
from twisted.python.failure import Failure

from util.state import Network
from util.client import BurlyBot
from util.db import Query
from util.event import Event
from util.threads import call_in_reactor
from util.types import DatabaseParams

reactor: Any = _reactor
log = getLogger(__name__)
EventTypes: TypeAlias = str | set[str] | list[str] | tuple[str, ...] | None


class TimeoutException(Exception):
    pass


class WaitData:
    def __init__(self, interestede: EventTypes, stope: EventTypes) -> None:
        # Cannot both be empty
        if not (interestede or stope):
            raise ValueError("WaitData requires an interested or stop event.")
        self.done = False
        self.q: Queue[Event] = Queue()
        self.interestede: set[str | None]
        self.stope: set[str | None]

        # Coerce a string to a set and force lower
        # all event_types are lower in lookups/dispatch
        if isinstance(interestede, (set, list, tuple)):
            self.interestede = set(x.lower() for x in interestede)
        elif isinstance(interestede, str):
            self.interestede = {interestede.lower()}
        else:
            self.interestede = {None}

        if isinstance(stope, (set, list, tuple)):
            self.stope = set(x.lower() for x in stope)
        elif isinstance(stope, str):
            self.stope = {stope.lower()}
        else:
            self.stope = {None}


class Container:
    # BurlyBot methods that should be waited on, and returned value of
    BLOCKINGCALLS = frozenset(
        {
            BurlyBot.checkSendMsg.__name__,
            BurlyBot.assembleMsgWLen.__name__,
            BurlyBot.calcAvailableMsgLength.__name__,
        }
    )
    IRC_API = frozenset(
        {
            "assembleMsgWLen",
            "calcAvailableMsgLength",
            "checkSendMsg",
            "join",
            "kick",
            "leave",
            "mode",
            "nickname",
            "notice",
            "sendmsg",
            "setNick",
        }
    )

    def __init__(self, settings: Any) -> None:
        self.network = settings.serverlabel
        self._settings = settings
        self.state = Network(settings.serverlabel)
        self._botinst: BurlyBot | None = None
        self._outqueue: deque[tuple[str, tuple[Any, ...], dict[str, Any]]] = deque()

    # TODO: instead of just redirecting the attribute lookup to _botinst maybe we should
    # define an explicit API and only allow access to those attributes, rather than
    # allowing access to everything in _botinst, like callbacks and stuff
    def __getattr__(self, name: str) -> Any:
        if name not in self.IRC_API:
            raise AttributeError("Bot API does not expose %s" % name)
        attr = getattr(BurlyBot, name)  # raise if the declared API is stale
        if self._botinst:
            attr = getattr(self._botinst, name)
            if callable(attr):
                # check if we need to blocking call or not:
                if name in self.BLOCKINGCALLS:
                    return partial(call_in_reactor, attr)
                else:
                    return partial(reactor.callFromThread, attr)
            else:
                return attr
        if callable(attr):
            if name in self.BLOCKINGCALLS:
                # pure query calls: nothing useful to queue while disconnected,
                # callers receive None (matching what queueing returned anyway)
                return lambda *a, **kw: None
            # return function to queue the real method call
            return partial(reactor.callFromThread, self._queuer, name)
        raise ValueError("Bot not connected.")

    def _queuer(self, funcname: str, *args: Any, **kwargs: Any) -> None:
        self._outqueue.append((funcname, args, kwargs))

    # say needs a source (channel, user, etc.) A source is supplied in BotWrapper
    def say(self, msg: Any) -> NoReturn:
        raise ValueError("No source defined.")

    def _setBotinst(self, botinst: BurlyBot | None) -> None:
        self._botinst = botinst
        # checkqueue 2 seconds after signedOn to give time to join channels and stablize
        # also keep checking this outqueue 2 seconds later if there is still elements left
        reactor.callLater(2, self._checkQueue)

    def _checkQueue(self) -> None:
        checkAgain = False
        if self._botinst:
            while self._outqueue:
                outbound = self._outqueue.popleft()
                log.info("processing queued bot API call: %s", outbound[0])
                # These will always be BurlyBot functions so let's do some magic.
                # There shouldn't be any AttributeError, and if there is, bad luck I guess.
                # This should always be called from inside the reactor so don't need to pass it to the reactor
                getattr(self._botinst, outbound[0])(*outbound[1], **outbound[2])
                checkAgain = True
        # check again in case we missed some
        if checkAgain:
            reactor.callLater(2, self._checkQueue)

    # Option getter/setters
    def getOption(self, opt: str, **kwargs: Any) -> Any:
        return self._settings.getOption(opt, **kwargs)

    def getOptions(self, opts: Iterable[str], **kwargs: Any) -> list[Any]:
        return self._settings.getOptions(opts, **kwargs)

    def setOption(self, opt: str, value: Any, **kwargs: Any) -> None:
        call_in_reactor(self._settings.setOption, opt, value, **kwargs)

    # Some module helpers
    def getModule(self, modname: str) -> ModuleType:
        return call_in_reactor(self._settings.getModule, modname)

    def isModuleAvailable(self, modname: str) -> bool:
        return self._settings.isModuleAvailable(modname)

    def _getCommandMappings(self, command: str | None = None) -> list[Any]:
        dispatcher = self._settings.dispatcher
        if dispatcher is None:
            return []
        return dispatcher._getCommandMappings(command)

    def getCommandMappings(self, command: str | None = None) -> list[Any]:
        return call_in_reactor(self._getCommandMappings, command)

    def _reloadModules(self, save_options: bool) -> None:
        if save_options:
            self._settings.saveOptions()
        self._settings.reload_current_modules()

    def reloadModules(self, *, save_options: bool = False) -> None:
        """Reload every module on this server; save_options=True writes the
        configuration file first (in the same reactor turn)."""
        call_in_reactor(self._reloadModules, save_options)

    # Event posting. Lets modules raise their own events (any type name) which
    # are dispatched exactly like IRC events: to Mapping(types=[...]) handlers
    # and to send_and_wait() generators. Handlers receive the plain Container
    # (no reply target) unless the poster supplies target/nick kwargs, so
    # bot.say() is not available to them by default.
    # Dispatch always happens on a later reactor turn, so an event posted from
    # init() during a (re)load is seen by every module once loading finishes.
    def _postEvent(
        self, event_type: str, broadcast: bool, eventkwargs: dict[str, Any]
    ) -> None:
        if broadcast:
            containers = [
                server.container for server in self._settings.servers.values()
            ]
        else:
            containers = [self]
        for container in containers:
            dispatcher = container._settings.dispatcher
            if dispatcher is None:
                continue
            # dispatch mutates its kwargs: give every server its own copy
            dispatcher.dispatchEvent(container, event_type, **dict(eventkwargs))

    def postEvent(
        self,
        event_type: str,
        *,
        broadcast: bool = False,
        **eventkwargs: Any,
    ) -> None:
        """Post an event of ``event_type`` to this server's dispatcher.

        broadcast=True posts to every connected server. Keyword arguments become
        Event attributes; pass authorized=True only when the source really is
        trusted (see Event.authorized). An event_id (shared across a broadcast)
        is added unless supplied. Fire-and-forget: returns immediately.
        """
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type must be a non-empty string.")
        if "type" in eventkwargs or "encoding" in eventkwargs:
            raise ValueError("type and encoding are reserved event attributes.")
        # shared by every per-server copy of a broadcast, so handlers of
        # process-wide actions (e.g. reload) can act once per post
        eventkwargs.setdefault("event_id", uuid4().hex)
        reactor.callFromThread(self._postEvent, event_type, broadcast, eventkwargs)

    def getAddon(self, addonname: str) -> Callable[..., Any]:
        return call_in_reactor(self._settings.getAddon, addonname)

    # callback to handle module errors
    # TODO: maybe provide modules a way to hook these?
    #     like if we let a module provide a function, we can pass the Failure object to it.
    def _moduleerr(self, e: Any) -> None:
        # docs seem to suggest this is always a Failure instance...
        if isinstance(e, Failure):
            e.cleanFailure()
            log.error("module handler failed:\n%s", e.getTraceback())
        else:
            log.error("module handler failed: %s", e)

    # stop event optional since you can just bail out of the generator if you know you have all
    # the things you want
    # f is the send function you want to call to start the waiting
    # Cleanup runs in the finally below, so bailing out of (or raising inside)
    # the consuming loop is safe once the generator is closed/GC'd. If your
    # function keeps running long after bailing, generator.close() promptly.
    def send_and_wait(
        self,
        interestede: EventTypes,
        stope: EventTypes = (),
        timeout: int | float = 10,
        f: Callable[..., Any] | None = None,
        fargs: Sequence[Any] = (),
        **kwargs: Any,
    ) -> Generator[Event, None, None]:
        """This method will block and yield events as they come..."""
        if not f:
            raise ValueError("Missing function")
        expired = time() + timeout
        while not self._botinst:
            if expired < time():
                raise TimeoutException()
            sleep(0.5)

        wd = WaitData(interestede, stope)

        try:
            # add wait events to dispatcher. ONLY MODIFY DISPATCHER IN REACTOR THREAD PLEASE.
            reactor.callFromThread(self._settings.dispatcher.addWaitData, wd)
            # send...
            f(*fargs, **kwargs)
            # and now we play the waiting game...
            # timeout is "run for this long total" (never resets on events);
            # open questions about hung-thread failsafes are tracked in #59
            while not wd.done:
                try:
                    item = wd.q.get(timeout=0.5)
                    yield item
                except Empty:
                    if expired < time():
                        raise TimeoutException() from None
            while not wd.q.empty():
                try:
                    yield wd.q.get()
                except Empty:
                    break
            return
        finally:
            # in the case that garbage collection happens (in the event that user bails the generator
            #     before the stop event fires) we can "clean up" and remove the event from the waitdispatcher
            reactor.callFromThread(self._settings.dispatcher.delWaitData, wd)

    # DB methods
    def dbQuery(
        self,
        q: str,
        params: DatabaseParams = (),
        func: Callable[..., Any] | None = None,
    ) -> Any:
        return self._settings.databasemanager.query(self.network, q, params, func)

    def dbBatch(self, qs: Sequence[Query]) -> list[Any]:
        """Run a series of queries back to back without possibility of being interupted.
        qs is an iterable of (query, params)"""
        return self._settings.databasemanager.batch(self.network, qs)

    def dbCheckCreateTable(self, tablename: str, createstmt: str) -> bool:
        return self._settings.databasemanager.dbCheckCreateTable(
            self.network, tablename, createstmt
        )

    # helper for modules: run callable in the reactor thread after delay seconds.
    # Bot API methods (bot.say, bot.sendmsg, ...) are safe to pass.
    def later(
        self, delay: int | float, callable: Callable[..., Any], *args: Any, **kw: Any
    ) -> None:
        reactor.callFromThread(reactor.callLater, delay, callable, *args, **kw)
