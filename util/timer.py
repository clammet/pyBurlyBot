from collections.abc import Callable
from typing import Any, ClassVar
# timer.py

# suggested use would be for an alarm module or somesuch.

from twisted.internet import reactor
from twisted.internet.task import LoopingCall

from .threads import call_in_reactor


class TimerExists(Exception):
    pass


class TimerInvalidName(Exception):
    pass


class TimerNotFound(Exception):
    pass


class Timer:
    # reps <= 0 means forever
    def __init__(
        self,
        name: str,
        interval: float,
        f: Callable[..., Any],
        reps: int = 1,
        startnow: bool = False,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.name = name
        self.f = f
        self.kwargs = kwargs
        self.args = args
        self.reps = reps
        self.interval = interval
        self.lc = LoopingCall(Timers.runTimer, self)
        self._startnow = startnow

    def start(self) -> None:
        self.lc.start(self.interval, self._startnow)

    def restart(self) -> None:
        try:
            self.lc.stop()
        except AssertionError:
            pass
        self.lc.start(self.interval, now=True)


class TimerInfo:
    def __init__(self, timer: Timer) -> None:
        self.name = timer.name
        self.f = timer.f
        self.kwargs = timer.kwargs
        self.reps = timer.reps
        self.interval = timer.interval


class Timers:
    timers: ClassVar[dict[str, Timer]] = {}

    # timers are managed in the reactor thread; callable from any thread
    @classmethod
    def _callInReactor(cls, f: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return call_in_reactor(f, *args, **kwargs)

    @classmethod
    def _addTimer(
        cls,
        name: str,
        interval: float,
        f: Callable[..., Any],
        reps: int = 1,
        startnow: bool = False,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        if name in cls.timers:
            raise TimerExists("Timer (%s) already exists." % name)
        else:
            timer = Timer(name, interval, f, reps, startnow, *args, **kwargs)
            # register before starting: startnow=True runs the timer callback
            # synchronously, which must be able to see (and expire) this entry
            cls.timers[name] = timer
            timer.start()
            return True

    # _timers are for internal use only
    @classmethod
    def addtimer(
        cls,
        name: str,
        interval: int | float,
        f: Callable[..., Any],
        reps: int = 1,
        startnow: bool = False,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        # kinda want to use _ prefix for internal things like DBcommit
        if not isinstance(name, str) or name.startswith("_"):
            raise TimerInvalidName("Invalid name (%s)." % name)
        # force interval and rep into float and int respectively.
        return cls._callInReactor(
            cls._addTimer,
            name,
            float(interval),
            f,
            int(reps),
            startnow,
            *args,
            **kwargs,
        )

    @classmethod
    def _deltimer(cls, name: str) -> bool:
        if name in cls.timers:
            # maybe add tryexcept here incase timer already finished
            try:
                cls.timers[name].lc.stop()
            except AssertionError:
                pass
            del cls.timers[name]
            return True
        else:
            raise TimerNotFound("Timer (%s) not found." % name)

    @classmethod
    def deltimer(cls, name: str) -> bool:
        if not isinstance(name, str) or name.startswith("_"):
            raise TimerInvalidName("Invalid name (%s)." % name)
        return cls._callInReactor(cls._deltimer, name)

    @classmethod
    def _restarttimer(cls, name: str) -> None:
        if name in cls.timers:
            cls.timers[name].restart()
        else:
            raise TimerNotFound("Timer (%s) not found." % name)

    @classmethod
    def restarttimer(cls, name: str) -> None:
        if not isinstance(name, str) or name.startswith("_"):
            raise TimerInvalidName("Invalid name (%s)." % name)
        return cls._callInReactor(cls._restarttimer, name)

    # run the desired function in a thread but manage the timer in the reactor
    @classmethod
    def runTimer(cls, timerobj: Timer) -> None:
        reactor.callInThread(timerobj.f, *timerobj.args, **timerobj.kwargs)
        if timerobj.reps > 0:
            timerobj.reps -= 1
            if timerobj.reps == 0:
                timerobj.lc.stop()
                cls.timers.pop(timerobj.name, None)

    @classmethod
    def _stopall(cls) -> None:
        for timername in cls.timers:
            try:
                cls.timers[timername].lc.stop()
            except AssertionError:
                continue

    @classmethod
    def _getTimers(cls) -> dict[str, TimerInfo]:
        d = {}
        for t in cls.timers:
            d[t] = TimerInfo(cls.timers[t])
        return d

    @classmethod
    def getTimers(cls) -> dict[str, TimerInfo]:
        return cls._callInReactor(cls._getTimers)

    @classmethod
    def _delPrefix(cls, prefix: str) -> None:
        for timername in list(cls.timers.keys()):
            if timername.startswith(prefix):
                try:
                    cls.timers[timername].lc.stop()
                except AssertionError:
                    pass
                del cls.timers[timername]

    @classmethod
    def delPrefix(cls, prefix: str) -> None:
        return cls._callInReactor(cls._delPrefix, prefix)
