from collections.abc import Iterable
from typing import Any

# This seems like a bit of a waste, but it's difficult to implement this in Container
#  because of the reliance on event data.
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread
from twisted.python.failure import Failure

from traceback import format_tb

from .container import Container
from .event import Event


class BotWrapper:
    def __init__(self, event: Event, botcont: Container) -> None:
        self.event = event
        self._botcont = botcont

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(
                "Private bot attributes are not part of the module API."
            )
        return getattr(self._botcont, name)

    # the sender's nick for a PM, else the channel the event came from
    def _replyTarget(self) -> str | None:
        return self.event.nick if self.event.isPM() else self.event.target

    # I think say should act as like a "reply" sending message back to whatever
    #  send it, be it channel or user
    # TODO: should this prepend event.nick so like "Nick, msg" "Nick: msg"?
    #         saves modules doing it every line. Maybe add a bypass?
    def say(self, msg: str, **kwargs: Any) -> None:
        self.sendmsg(self._replyTarget(), msg, **kwargs)

    def checkSay(self, msg: str) -> bool:
        return self.checkSendMsg(self._replyTarget(), msg)

    def isadmin(
        self, module: str | None = None, inreactor: bool = False
    ) -> bool | None:
        if inreactor:
            return self._isadmin(module)
        else:
            # blockingCallFromThread waits on a returned Deferred
            return blockingCallFromThread(reactor, self._isadmin, module, True)

    # _isadmin bypasses the containers get*Option methods so that it
    # only makes 1 call in the reactor and not 2 (in the case of module admin)
    def _isadmin(self, module: str | None = None, blocking: bool = False) -> Any:
        if not self.event.nick:
            return None
        admins = self._botcont._settings.getOption("admins", inreactor=True)
        if module:
            madmins = None
            try:
                madmins = self._botcont._settings.getOption(
                    "admins", module=module, inreactor=True
                )
            except AttributeError:
                pass
            if madmins:
                admins.extend(madmins)
        settings = self._botcont._settings
        nick, account = self.event.nick, self.event.account
        botinst = self._botcont._botinst
        # No IRCv3 account identity on this network: ask NickServ (only possible
        # when the caller can wait on the reply, i.e. from a module thread).
        if (
            blocking
            and account is None
            and botinst is not None
            and getattr(botinst, "_legacy_account_lookup", False)
        ):
            d = botinst.resolveLegacyAccount(nick)
            d.addCallback(lambda resolved: settings.is_admin(nick, resolved, admins))
            return d
        return settings.is_admin(nick, account, admins)

    # option getter/setters
    # if channel is None, default module options to the current channel;
    # core options have no channel scope so they are left unscoped.
    # otherwise duplicated from container
    def _defaultChannel(
        self, channel: str | bool | None, module: str | None
    ) -> str | bool | None:
        if channel is None and module and not self.event.isPM():
            return self.event.target
        return channel

    def getOption(
        self, opt: str, channel: str | bool | None = None, **kwargs: Any
    ) -> Any:
        channel = self._defaultChannel(channel, kwargs.get("module"))
        return self._botcont._settings.getOption(opt, channel=channel, **kwargs)

    def getOptions(
        self, opts: Iterable[str], channel: str | bool | None = None, **kwargs: Any
    ) -> list[Any]:
        channel = self._defaultChannel(channel, kwargs.get("module"))
        return self._botcont._settings.getOptions(opts, channel=channel, **kwargs)

    # Default target channel for setOption is to target current channel unless argument of "channel" is False
    def setOption(
        self,
        opt: str,
        value: Any,
        channel: str | bool | None = None,
        inreactor: bool = False,
        **kwargs: Any,
    ) -> None:
        channel = self._defaultChannel(channel, kwargs.get("module"))
        if inreactor:
            self._botcont._settings.setOption(opt, value, channel=channel, **kwargs)
        else:
            blockingCallFromThread(
                reactor,
                self._botcont._settings.setOption,
                opt,
                value,
                channel=channel,
                **kwargs,
            )

    # callback to handle module errors
    def _moduleerr(self, e: Any) -> None:
        if isinstance(e, Failure):
            e.cleanFailure()
            e.printTraceback()
            tb = e.getTracebackObject()
            ex = e.value
            if tb:
                # The (hopefully) most 2 important stacks from the traceback.
                # The first 2 are from twisted, the next one is the module stack, probably, and then the next one is whatever the
                # module called.
                self.say(
                    "%s: %s. %s"
                    % (
                        type(ex).__name__,
                        ex,
                        "| ".join(format_tb(tb, 5)[-2:]).replace("\n", ". "),
                    )
                )
            else:
                self.say(
                    "%s: %s. Don't know where, check log." % (type(ex).__name__, ex)
                )
        else:
            self.say("Error: %s" % str(e))
            print("error:", e)
