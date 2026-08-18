from collections.abc import Callable, Iterable, Sequence
from typing import Any, NoReturn, cast
from base64 import b64encode
from sys import exc_info

# twisted imports
from twisted.words.protocols.irc import (
    IRCClient,
    IRCBadMessage,
    IRCBadModes,
    parseModes,
    X_DELIM,
    symbolic_to_numeric,
    numeric_to_symbolic,
    ctcpExtract,
    lowDequote,
    lowQuote,
    parsemsg,
)
from twisted.internet import reactor as _reactor
from twisted.internet.defer import Deferred
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.python import log
from twisted.python.failure import Failure
from twisted.protocols.basic import LineReceiver
from twisted.protocols.policies import TimeoutMixin
from OpenSSL import SSL

# system imports
from logging import getLogger
from time import time
from collections import deque
from math import floor

# BurlyBot imports
from .helpers import (
    irc_casefold,
    processHostmask,
    processListReply,
    PrefixMap,
    isIterable,
    splitEncodedUnicode,
)
from .state import Network

logger = getLogger(__name__)

reactor: Any = _reactor


def _tlsConnectionErrorHint(settings: Any, reason: Any) -> str | None:
    if not getattr(settings, "ssl", False) or not reason.check(SSL.Error):
        return None
    return (
        "[TLS connection hint: check that the configured port accepts direct TLS; "
        f"attempted {settings.host}:{settings.port}.]"
    )


# inject some other common symbolic IDs:
symbolic_to_numeric["RPL_YOURID"] = "042"
symbolic_to_numeric["RPL_LOCALUSERS"] = "265"
symbolic_to_numeric["RPL_GLOBALUSERS"] = "266"
symbolic_to_numeric["RPL_CREATIONTIME"] = "329"
symbolic_to_numeric["RPL_HOSTHIDDEN"] = "396"
symbolic_to_numeric["ERR_NOTEXTTOSEND"] = "412"
symbolic_to_numeric["RPL_LOGGEDIN"] = "900"
symbolic_to_numeric["RPL_SASLSUCCESS"] = "903"
symbolic_to_numeric["ERR_SASLFAIL"] = "904"
symbolic_to_numeric["ERR_SASLTOOLONG"] = "905"
symbolic_to_numeric["ERR_SASLABORTED"] = "906"
symbolic_to_numeric["ERR_SASLALREADY"] = "907"
# and the reverse:
numeric_to_symbolic["042"] = "RPL_YOURID"
numeric_to_symbolic["265"] = "RPL_LOCALUSERS"
numeric_to_symbolic["266"] = "RPL_GLOBALUSERS"
numeric_to_symbolic["329"] = "RPL_CREATIONTIME"
numeric_to_symbolic["396"] = "RPL_HOSTHIDDEN"
numeric_to_symbolic["412"] = "ERR_NOTEXTTOSEND"
for _sasl_symbol, _sasl_numeric in tuple(symbolic_to_numeric.items()):
    if _sasl_numeric in {"900", "903", "904", "905", "906", "907"}:
        numeric_to_symbolic[_sasl_numeric] = _sasl_symbol


def _unescape_message_tag(value: str) -> str:
    replacements = {":": ";", "s": " ", "r": "\r", "n": "\n", "\\": "\\"}
    result: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            i += 1
            result.append(replacements.get(value[i], value[i]))
        else:
            result.append(value[i])
        i += 1
    return "".join(result)


def _parse_message_tags(line: str) -> tuple[dict[str, str | None], str]:
    if not line.startswith("@"):
        return {}, line
    tag_data, line = line[1:].split(" ", 1)
    tags: dict[str, str | None] = {}
    for item in tag_data.split(";"):
        key, separator, value = item.partition("=")
        tags[key] = _unescape_message_tag(value) if separator else None
    return tags, line


class BurlyBot(IRCClient, TimeoutMixin):
    """BurlyBot"""

    timeOut = 150  # 2.5mins

    erroneousNickFallback = "BurlyBot"
    linethrottle = 3
    _lines = 0
    _lastmsg: float = 0
    _lastCL: Any = None
    supported: Any = None
    altindex = 0
    prefixlen = None
    delimiter = b"\n"  # LineReceiver adds this after _reallySendLine appends CR.
    versionName: Any = "pyBurlyBot git"
    realname: Any = "Burly Bot"
    settings: Any
    debug: int
    state: Network | None
    dispatch: Callable[..., bool]
    dispatcher: Any
    container: Any
    _dqueue: deque[str]
    _names: dict[str, list[str]]
    _banlist: dict[str, list[tuple[str, str, str, str | None]]]
    _exceptlist: dict[str, list[tuple[str, str, str, str | None]]]
    _invitelist: dict[str, list[tuple[str, str, str, str | None]]]
    _accounts: dict[str, str]
    # Legacy (no IRCv3 account caps) identity: NickServ STATUS lookups.
    _legacy_account_lookup: bool = False
    _status_cache: dict[str, tuple[float, str | None]]
    _status_pending: dict[str, list[Deferred[str | None]]]
    _status_timeouts: dict[str, Any]
    legacy_status_timeout: float = 10.0
    legacy_status_ttl: float = 60.0
    legacy_status_negative_ttl: float = 5.0

    # http://twistedmatrix.com/trac/browser/trunk/twisted/words/protocols/irc.py
    # irc_ and RPL_ methods are duplicated here verbatim so that we can dispatch higher level
    # events with the low level data intact.

    # custom sendline throttler. This might be overly complex but should behave similar to mIRC
    # where lines are only throttled once you cross a threshold. I don't know if the cooldown is similar though
    def sendLine(self, line: str | bytes) -> None:
        if isinstance(line, bytes):
            line = line.decode(self.settings.encoding, "replace")
        encoded = line.encode(self.settings.encoding)
        if len(encoded) > 510:
            line = encoded[:510].decode(self.settings.encoding, "ignore")
        t = time()
        if self._dqueue:
            # lines already queued: append so output order is preserved
            self._dqueue.append(line)
            if not self._lastCL:
                self._lastCL = reactor.callLater(1.0, self._sendLine)
        elif self._lastmsg + 1 < t:
            # if message hasn't been sent for 1 seconds, go for it
            self._lines += 1
            self._reallySendLine(line)
            # also reset the linecount if no msg for 2 seconds
            if self._lastmsg + 2 < t:
                self._lines = 1
        elif self._lines < self.linethrottle:
            # under threshold, go for it
            self._lines += 1
            self._reallySendLine(line)
        else:
            # cross threshold in 1 second, slow down
            self._dqueue.append(line)
            if not self._lastCL:
                self._lastCL = reactor.callLater(1.0, self._sendLine)
        self._lastmsg = t

    def _sendLine(self) -> None:
        t = time()
        if self._dqueue:
            line = self._dqueue.popleft()
            self._reallySendLine(line)
            self._lastmsg = t
            if self._dqueue:
                self._lastCL = reactor.callLater(1.0, self._sendLine)
            else:
                self._lastCL = None
        else:
            self._lastCL = None

    # sticking to specification
    def _reallySendLine(self, line: str) -> None:
        quoted_line = lowQuote(line).encode(self.settings.encoding) + b"\r"
        if self.debug >= 2:
            displayed = (
                b"AUTHENTICATE <redacted>\r"
                if line.startswith("AUTHENTICATE ")
                else quoted_line
            )
            logger.debug("REALLY SENDING LINE: %r", displayed + self.delimiter)
        return LineReceiver.sendLine(self, quoted_line)

    def register(
        self, nickname: str, hostname: str = "foo", servername: str = "bar"
    ) -> None:
        """Register and negotiate IRCv3 account identity plus optional SASL PLAIN."""
        username = getattr(self.settings, "sasl_username", None)
        password = getattr(self.settings, "sasl_password", None)
        self._sasl_required = bool(username and password)
        self._capabilities: set[str] = set()
        self._cap_ls: set[str] = set()
        self._cap_values: dict[str, str | None] = {}
        self._cap_ended = False
        self._sasl_payload_sent = False
        self._legacy_account_lookup = False
        self._reallySendLine("CAP LS 302")
        if self.password is not None:
            self._reallySendLine("PASS %s" % self.password)
        self._attemptedNick = nickname
        self._reallySendLine("NICK %s" % nickname)
        if self.username is None:
            self.username = nickname
        self._reallySendLine(
            "USER {} {} {} :{}".format(
                self.username, hostname, servername, self.realname
            )
        )

    def _end_cap(self) -> None:
        if not self._cap_ended:
            if not self._capabilities.intersection(
                {"account-notify", "account-tag", "extended-join"}
            ):
                self._legacy_account_lookup = True
                logger.warning(
                    "server %s did not enable IRC account identity; "
                    "administrator commands will be verified with NickServ STATUS "
                    "(admins are matched by identified nickname).",
                    self.settings.serverlabel,
                )
            self._cap_ended = True
            self._reallySendLine("CAP END")

    def _fail_sasl(self, reason: str) -> None:
        log.msg("SASL authentication failed: %s" % reason, isError=True)
        self._reallySendLine("QUIT :SASL authentication failed")
        cast(Any, self.transport).loseConnection()

    def irc_CAP(self, prefix: str, params: list[str]) -> None:
        if len(params) < 2:
            return
        subcommand = params[1].upper()
        continuation = len(params) > 2 and params[2] == "*"
        capability_text = params[-1] if len(params) > 2 else ""
        capability_items = [item.lstrip("-") for item in capability_text.split()]
        capabilities = {item.split("=", 1)[0] for item in capability_items}
        if subcommand == "LS":
            self._cap_ls.update(capabilities)
            for item in capability_items:
                name, separator, value = item.partition("=")
                self._cap_values[name] = value if separator else None
            if continuation:
                return
            desired = self._cap_ls.intersection(
                {"account-notify", "account-tag", "extended-join"}
            )
            if self._sasl_required:
                if "sasl" not in self._cap_ls:
                    self._fail_sasl("server does not advertise SASL")
                    return
                mechanisms = self._cap_values.get("sasl")
                if mechanisms and "PLAIN" not in mechanisms.upper().split(","):
                    self._fail_sasl("server does not advertise SASL PLAIN")
                    return
                desired.add("sasl")
            if desired:
                self._reallySendLine("CAP REQ :%s" % " ".join(sorted(desired)))
            else:
                self._end_cap()
        elif subcommand == "ACK":
            self._capabilities.update(capabilities)
            if "sasl" in capabilities and self._sasl_required:
                self._reallySendLine("AUTHENTICATE PLAIN")
            else:
                self._end_cap()
        elif subcommand == "NAK":
            if self._sasl_required and "sasl" in capabilities:
                self._fail_sasl("server rejected the SASL capability")
            else:
                self._end_cap()

    def irc_AUTHENTICATE(self, prefix: str, params: list[str]) -> None:
        if not params or params[0] != "+" or self._sasl_payload_sent:
            return
        authcid = self.settings.sasl_username
        authzid = self.settings.sasl_authzid or ""
        payload = b64encode(
            ("%s\0%s\0%s" % (authzid, authcid, self.settings.sasl_password)).encode(
                "utf-8"
            )
        ).decode("ascii")
        for offset in range(0, len(payload), 400):
            self._reallySendLine("AUTHENTICATE %s" % payload[offset : offset + 400])
        if len(payload) % 400 == 0:
            self._reallySendLine("AUTHENTICATE +")
        self._sasl_payload_sent = True

    def irc_RPL_SASLSUCCESS(self, prefix: str, params: list[str]) -> None:
        self._end_cap()

    def irc_ERR_SASLFAIL(self, prefix: str, params: list[str]) -> None:
        self._fail_sasl(params[-1] if params else "authentication rejected")

    irc_ERR_SASLTOOLONG = irc_ERR_SASLFAIL
    irc_ERR_SASLABORTED = irc_ERR_SASLFAIL

    def irc_ERR_SASLALREADY(self, prefix: str, params: list[str]) -> None:
        self._end_cap()

    def _account_for(self, prefix: str) -> str | None:
        nick, _, _ = processHostmask(prefix)
        if nick is None:
            return None
        if "account" in self._message_tags:
            account = self._message_tags["account"]
            if account and account != "*":
                self._accounts[nick.casefold()] = account
                return account
            self._accounts.pop(nick.casefold(), None)
            return None
        return self._accounts.get(nick.casefold())

    ###
    ### Legacy account identity (networks without IRCv3 account capabilities)
    ###
    def _legacyStatusState(
        self,
    ) -> tuple[
        dict[str, tuple[float, str | None]],
        dict[str, list[Deferred[str | None]]],
        dict[str, Any],
    ]:
        if not hasattr(self, "_status_cache"):
            self._status_cache = {}
            self._status_pending = {}
            self._status_timeouts = {}
        return self._status_cache, self._status_pending, self._status_timeouts

    def _needsLegacyAccount(self, event_type: str, msg: str | None) -> bool:
        """True when an admin command needs a NickServ STATUS check before dispatch."""
        if not self._legacy_account_lookup or not msg:
            return False
        dispatcher = getattr(self, "dispatcher", None)
        if dispatcher is None:
            return False
        return bool(dispatcher.isAdminCommand(event_type, msg))

    def _forgetLegacyAccount(self, nick: str) -> None:
        cache, _, _ = self._legacyStatusState()
        cache.pop(irc_casefold(nick), None)

    def resolveLegacyAccount(self, nick: str) -> Deferred[str | None]:
        """Resolve a nick's services identity via NickServ STATUS.

        Fires with the nick (as reported by services) when the user is identified
        (STATUS level 3), otherwise with None. Results are cached briefly.
        """
        cache, pending, timeouts = self._legacyStatusState()
        key = irc_casefold(nick)
        now = time()
        cached = cache.get(key)
        if cached and cached[0] > now:
            d: Deferred[str | None] = Deferred()
            d.callback(cached[1])
            return d
        d = Deferred()
        waiters = pending.get(key)
        if waiters is not None:
            waiters.append(d)
            return d
        pending[key] = [d]
        timeouts[key] = reactor.callLater(
            self.legacy_status_timeout, self._legacyStatusTimeout, key
        )
        self.sendLine("PRIVMSG NickServ :STATUS %s" % nick)
        return d

    def _legacyStatusTimeout(self, key: str) -> None:
        _, _, timeouts = self._legacyStatusState()
        timeouts.pop(key, None)
        log.msg("NickServ STATUS lookup for %s timed out" % key, isError=True)
        self._finishLegacyStatus(key, None, cache_result=False)

    def _finishLegacyStatus(
        self, key: str, account: str | None, cache_result: bool = True
    ) -> None:
        cache, pending, timeouts = self._legacyStatusState()
        call = timeouts.pop(key, None)
        if call is not None and call.active():
            call.cancel()
        if cache_result:
            ttl = self.legacy_status_ttl if account else self.legacy_status_negative_ttl
            cache[key] = (time() + ttl, account)
        for d in pending.pop(key, ()):
            d.callback(account)

    def _abandonLegacyStatus(self) -> None:
        """Drop pending STATUS lookups (connection gone); waiters get None."""
        _, pending, _ = self._legacyStatusState()
        for key in list(pending):
            self._finishLegacyStatus(key, None, cache_result=False)

    def _handleLegacyStatusReply(self, prefix: str, message: str) -> None:
        """Consume ``STATUS <nick> <level>`` notices from NickServ."""
        sender, _, _ = processHostmask(prefix)
        if sender is None or sender.casefold() != "nickserv":
            return
        parts = message.split()
        if len(parts) < 3 or parts[0].upper() != "STATUS" or not parts[2].isdigit():
            return
        nick, level = parts[1], int(parts[2])
        self._finishLegacyStatus(irc_casefold(nick), nick if level >= 3 else None)

    def _dispatchMessage(
        self, event_type: str, nick: str | None, msg: str, **kwargs: Any
    ) -> None:
        account = self._account_for(kwargs["prefix"])
        if account is None and nick and self._needsLegacyAccount(event_type, msg):
            d = self.resolveLegacyAccount(nick)
            d.addCallback(
                lambda resolved: self.dispatch(
                    self, event_type, msg=msg, nick=nick, account=resolved, **kwargs
                )
            )
            d.addErrback(log.err)
            return
        self.dispatch(self, event_type, msg=msg, nick=nick, account=account, **kwargs)

    def dataReceived(self, data: bytes) -> None:
        self.resetTimeout()
        IRCClient.dataReceived(self, data)

    def names(self, channels: str | Iterable[str]) -> None:
        """
        List the users in a channel.
        """
        if isIterable(channels):
            self.sendLine("NAMES %s" % ",".join(channels))
        else:
            self.sendLine("NAMES %s" % channels)

    def banlist(self, channel: str) -> None:
        self.mode(channel, True, "b")

    ###
    ### The following are "low level events" almost (probably, maybe butchered) verbatim from IRCClient
    ###
    def irc_JOIN(self, prefix: str, params: list[str]) -> None:
        """
        Called when a user joins a channel.
        """
        nick, ident, host = processHostmask(prefix)
        if nick is None:
            return
        channel = params[0]
        account = (
            params[1]
            if len(params) >= 3 and params[1] != "*"
            else self._account_for(prefix)
        )
        if account:
            self._accounts[nick.casefold()] = account
        if nick == self.nickname:
            # take note of our prefix! (for message length calculation
            self.prefixlen = len(prefix)
            if self.state:
                self.state._joinchannel(channel)
                self.sendLine("MODE %s" % channel)
            self.dispatch(
                self,
                "joined",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                target=channel,
                nick=nick,
                ident=ident,
                host=host,
                account=account,
            )
        else:
            if self.state:
                self.state._userjoin(channel, nick, ident, host, prefix)
            self.dispatch(
                self,
                "userJoined",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                target=channel,
                nick=nick,
                ident=ident,
                host=host,
                account=account,
            )

    def irc_PART(self, prefix: str, params: list[str]) -> None:
        """
        Called when a user leaves a channel.
        """
        nick, ident, host = processHostmask(prefix)
        if nick is None:
            return
        channel = params[0]
        if nick == self.nickname:
            # take note of our prefix! (for message length calculation
            self.prefixlen = len(prefix)
            if self.state:
                self.state._leavechannel(channel)
            self.dispatch(
                self,
                "left",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                target=channel,
                nick=nick,
                ident=ident,
                host=host,
            )
        else:
            if self.state:
                self.state._userpart(channel, nick, ident, host, prefix)
            self.dispatch(
                self,
                "userLeft",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                target=channel,
                nick=nick,
                ident=ident,
                host=host,
            )

    def irc_QUIT(self, prefix: str, params: list[str]) -> None:
        """
        Called when a user has quit.
        """
        nick, ident, host = processHostmask(prefix)
        if nick is None:
            return
        if self.state:
            self.state._userquit(nick)
        self._accounts.pop(nick.casefold(), None)
        self._forgetLegacyAccount(nick)
        self.dispatch(
            self,
            "userQuit",
            prefix=prefix,
            params=params,
            hostmask=prefix,
            msg=params[0],
            nick=nick,
            ident=ident,
            host=host,
        )

    def irc_MODE(self, prefix: str, params: list[str]) -> None:
        """
        Parse a server mode change message.
        """
        channel, modes, args = params[0], params[1], params[2:]

        if modes[0] not in "-+":
            modes = "+" + modes

        if channel == self.nickname:
            # This is a mode change to our individual user, not a channel mode
            # that involves us.
            paramModes = self.getUserModeParams()
            # take note of our prefix! (for message length calculation
            self.prefixlen = len(prefix)
        else:
            paramModes = self.getChannelModeParams()

        try:
            added, removed = parseModes(modes, args, paramModes)
        except IRCBadModes:
            log.err(
                None,
                "An error occured while parsing the following "
                "MODE message: MODE %s" % (" ".join(params),),
            )
        else:
            nick, ident, host = processHostmask(prefix)
            if self.state and (channel != self.nickname):
                self.state._modechange(channel, nick, added, removed)
            self.dispatch(
                self,
                "modeChanged",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                target=channel,
                added=added,
                removed=removed,
                modes=modes,
                args=args,
                nick=nick,
                ident=ident,
                host=host,
            )

    def irc_PRIVMSG(self, prefix: str, params: list[str]) -> None:
        """
        Called when we get a message.
        """
        if self.debug >= 2:
            logger.debug("INCOMING PRIVMSG: %s %s", prefix, params)
        user = prefix
        channel = params[0]
        message = params[-1]
        if not message:
            # Don't raise an exception if we get blank message.
            return

        if message[0] == X_DELIM:
            m = ctcpExtract(message)
            if m["extended"]:
                self.ctcpQuery(user, channel, m["extended"])

            if not m["normal"]:
                return

            message = " ".join(m["normal"])

        nick, ident, host = processHostmask(prefix)
        if nick == self.nickname:
            # take note of our prefix! (for message length calculation
            self.prefixlen = len(prefix)
        # These are actually messages, ctcp's aren't dispatched here
        self._dispatchMessage(
            "privmsged",
            nick,
            message,
            prefix=prefix,
            params=params,
            hostmask=user,
            target=channel,
            ident=ident,
            host=host,
        )

    def irc_NOTICE(self, prefix: str, params: list[str]) -> None:
        """
        Called when a user gets a notice.
        """
        if self.debug >= 2:
            logger.debug("INCOMING NOTICE: %s %s", prefix, params)
        user = prefix
        channel = params[0]
        message = params[-1]
        if not message:
            return

        if message[0] == X_DELIM:
            m = ctcpExtract(message)
            if m["extended"]:
                self.ctcpReply(user, channel, m["extended"])
            if not m["normal"]:
                return
            message = " ".join(m["normal"])

        nick, ident, host = processHostmask(prefix)
        if nick == self.nickname:
            # take note of our prefix! (for message length calculation
            self.prefixlen = len(prefix)
        if self._legacy_account_lookup:
            self._handleLegacyStatusReply(prefix, message)
        self._dispatchMessage(
            "noticed",
            nick,
            message,
            prefix=prefix,
            params=params,
            hostmask=user,
            target=channel,
            ident=ident,
            host=host,
        )

    def irc_NICK(self, prefix: str, params: list[str]) -> None:
        """
        Called when a user changes their nickname.
        """
        nick, ident, host = processHostmask(prefix)
        if nick is None:
            return
        account = self._accounts.pop(nick.casefold(), None)
        if account:
            self._accounts[params[0].casefold()] = account
        self._forgetLegacyAccount(nick)
        self._forgetLegacyAccount(params[0])

        if nick == self.nickname:
            # take note of our prefix! (for message length calculation
            self.prefixlen = len(prefix)
            self.nickChanged(params[0])
            self.dispatch(
                self,
                "nickChanged",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                newname=params[0],
            )
        else:
            if self.state:
                self.state._userrename(nick, params[0], ident, host, prefix)
            self.dispatch(
                self,
                "userRenamed",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                newname=params[0],
                nick=nick,
                ident=ident,
                host=host,
                account=account,
            )

    def irc_ACCOUNT(self, prefix: str, params: list[str]) -> None:
        nick, _, _ = processHostmask(prefix)
        if nick is None or not params:
            return
        if params[0] == "*":
            self._accounts.pop(nick.casefold(), None)
        else:
            self._accounts[nick.casefold()] = params[0]

    def irc_KICK(self, prefix: str, params: list[str]) -> None:
        """
        Called when a user is kicked from a channel.
        """
        kicker = prefix.split("!")[0]
        channel = params[0]
        kicked = params[1]
        message = params[-1]
        if kicked.lower() == self.nickname.lower():
            if self.state:
                self.state._leavechannel(channel)
            self.dispatch(
                self,
                "kickedFrom",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                nick=kicker,
                target=channel,
                msg=message,
                kicked=kicked,
            )
        else:
            if self.state:
                self.state._userpart(channel, kicked)
            self.dispatch(
                self,
                "userKicked",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                nick=kicker,
                target=channel,
                msg=message,
                kicked=kicked,
            )

    def irc_RPL_TOPIC(self, prefix: str, params: list[str]) -> None:
        """
        Called when the topic for a channel is initially reported or when it
        subsequently changes.
        """
        nick, ident, host = processHostmask(prefix)
        channel = params[1]
        newtopic = params[2]

        if self.state:
            self.state._settopic(channel, newtopic, nick, ident, host)
        self.dispatch(
            self,
            "topicUpdated",
            prefix=prefix,
            params=params,
            hostmask=prefix,
            target=channel,
            newtopic=newtopic,
            nick=nick,
            ident=ident,
            host=host,
        )

    def irc_RPL_NOTOPIC(self, prefix: str, params: list[str]) -> None:
        """
        ...
        """
        nick, ident, host = processHostmask(prefix)
        channel = params[1]
        newtopic = ""
        if self.state:
            self.state._settopic(channel, newtopic, nick, ident, host)
        self.dispatch(
            self,
            "topicUpdated",
            prefix=prefix,
            params=params,
            hostmask=prefix,
            target=channel,
            newtopic=newtopic,
            nick=nick,
            ident=ident,
            host=host,
        )

    def irc_RPL_ENDOFMOTD(self, prefix: str, params: list[str]) -> None:
        """
        Called when the bot receives RPL_ENDOFMOTD from the server.

        motd is a list containing the accumulated contents of the message of the day.
        """
        motd = self.motd
        if self.state:
            self.state.motd = motd
        # The following sets self.motd to None, so we get the motd first
        IRCClient.irc_RPL_ENDOFMOTD(self, prefix, params)
        self.dispatch(self, "receivedMOTD", prefix=prefix, params=params, motd=motd)

    def irc_RPL_MYINFO(self, prefix: str, params: list[str]) -> None:
        info: list[str | None] = list(params[1].split(None, 3))
        while len(info) < 4:
            info.append(None)
        servername, version, umodes, cmodes = info
        self.dispatch(
            self,
            "myInfo",
            prefix=prefix,
            params=params,
            servername=servername,
            version=version,
            umodes=umodes,
            cmodes=cmodes,
        )

    ### The following are custom, not taken from IRCClient:
    def irc_RPL_CHANNELMODEIS(self, prefix: str, params: list[str]) -> None:
        """
        Parse a RPL_CHANNELMODEIS message.
        """
        channel, modes, args = params[1], params[2], params[3:]

        if modes[0] not in "-+":
            modes = "+" + modes
        try:
            added, _ = parseModes(modes, args, self.getChannelModeParams())
        except IRCBadModes:
            log.err(
                None,
                "An error occured while parsing the following "
                "MODE message: MODE %s" % (" ".join(params),),
            )
        else:
            if self.state:
                self.state._modechange(channel, None, added, [], reset=True)
            self.dispatch(
                self,
                "channelModeIs",
                prefix=prefix,
                params=params,
                hostmask=prefix,
                target=channel,
                added=added,
                modes=modes,
                args=args,
            )

    def irc_RPL_CREATIONTIME(self, prefix: str, params: list[str]) -> None:
        channel = params[1]
        t = params[2]
        self.dispatch(
            self,
            "creationTime",
            prefix=prefix,
            params=params,
            target=channel,
            creationtime=t,
        )

    def irc_RPL_NAMREPLY(self, prefix: str, params: list[str]) -> None:
        """
        Called when NAMES reply is received from the server.
        """
        channel = params[2]
        users = params[3].split()
        # TODO: should we give this event a copy of PrefixMap? check state._addusers as for why
        if self.state:
            self.state._addusers(channel, users)
        self._names.setdefault(channel, []).extend(users)
        self.dispatch(
            self, "nameReply", prefix=prefix, params=params, target=channel, users=users
        )

    def irc_RPL_ENDOFNAMES(self, prefix: str, params: list[str]) -> None:
        channel = params[1]
        self.dispatch(
            self,
            "endOfNames",
            prefix=prefix,
            params=params,
            target=channel,
            users=self._names.pop(channel, []),
        )

    def irc_RPL_BANLIST(self, prefix: str, params: list[str]) -> None:
        """
        Called when RPL_BANLIST reply is received from the server.
        """
        channel, banmask, nick, ident, host, t, hostmask = processListReply(params)

        self._banlist.setdefault(channel, []).append((banmask, hostmask, t, nick))
        self.dispatch(
            self,
            "banList",
            prefix=prefix,
            params=params,
            target=channel,
            banmask=banmask,
            hostmask=hostmask,
            timeofban=t,
            nick=nick,
            ident=ident,
            host=host,
        )

    def irc_RPL_ENDOFBANLIST(self, prefix: str, params: list[str]) -> None:
        channel = params[1]
        banlist = self._banlist.pop(channel, [])
        if self.state:
            self.state._addbans(channel, banlist)
        self.dispatch(
            self,
            "endOfBanList",
            prefix=prefix,
            params=params,
            target=channel,
            banlist=banlist,
        )

    def irc_RPL_EXCEPTLIST(self, prefix: str, params: list[str]) -> None:
        """
        Called when RPL_EXCEPTLIST reply is received from the server.
        """
        channel, exceptmask, nick, ident, host, t, hostmask = processListReply(params)

        self._exceptlist.setdefault(channel, []).append((exceptmask, hostmask, t, nick))
        self.dispatch(
            self,
            "exceptList",
            prefix=prefix,
            params=params,
            target=channel,
            exceptmask=exceptmask,
            hostmask=hostmask,
            timeofban=t,
            nick=nick,
            ident=ident,
            host=host,
        )

    def irc_RPL_ENDOFEXCEPTLIST(self, prefix: str, params: list[str]) -> None:
        channel = params[1]

        exceptlist = self._exceptlist.pop(channel, [])
        if self.state:
            self.state._addexcepts(channel, exceptlist)
        self.dispatch(
            self,
            "endOfExceptList",
            prefix=prefix,
            params=params,
            target=channel,
            exceptlist=exceptlist,
        )

    def irc_RPL_INVITELIST(self, prefix: str, params: list[str]) -> None:
        """
        Called when RPL_INVITELIST reply is received from the server.
        """
        channel, invitemask, nick, ident, host, t, hostmask = processListReply(params)

        self._invitelist.setdefault(channel, []).append((invitemask, hostmask, t, nick))
        self.dispatch(
            self,
            "inviteList",
            prefix=prefix,
            params=params,
            target=channel,
            invitemask=invitemask,
            hostmask=hostmask,
            timeofban=t,
            nick=nick,
            ident=ident,
            host=host,
        )

    def irc_RPL_ENDOFINVITELIST(self, prefix: str, params: list[str]) -> None:
        channel = params[1]

        invitelist = self._invitelist.pop(channel, [])
        if self.state:
            self.state._addinvites(channel, invitelist)
        self.dispatch(
            self,
            "endOfInviteList",
            prefix=prefix,
            params=params,
            target=channel,
            invitelist=invitelist,
        )

    def irc_RPL_ISUPPORT(self, prefix: str, params: list[str]) -> None:
        IRCClient.irc_RPL_ISUPPORT(self, prefix, params)
        # This seems excessive but it's the only way to reliably update the prefixmap
        self.prefixmap.loadfromprefix(iter(self.supported.getFeature("PREFIX").items()))

    # This method is interesting, for example ERROR gets sent from Rizon when you quit
    # TODO: find out what to actually do with this.
    def irc_ERROR(self, prefix: str, params: list[str]) -> None:
        logger.error("ERROR received: %s", params)

    ###
    ### Modified command handler from IRCCLient
    ###
    def handleCommand(self, command: str, prefix: str, params: list[str]) -> None:
        """
        Determine the function to call for the given command and call it with
        the given arguments.
        """
        method_name = "irc_%s" % command.upper()
        method = getattr(self, method_name, None)
        try:
            if callable(method):
                method(prefix, params)
        except Exception:  # noqa: BLE001 - IRC callbacks are a protocol boundary
            log.deferr()
        else:
            # All low level (RPL_type) events dispatched as they are
            # These will either be numeric or symbolic, so we also dispatch the
            # corresponding symbolic/numeric event when possible for ease of use
            self.dispatch(self, command, prefix=prefix, params=params)
            # lineReceived already converted known numerics to symbolic, so
            # command here is symbolic (or an unknown numeric): dispatch the
            # numeric twin when one exists
            if command.upper() in symbolic_to_numeric:
                self.dispatch(
                    self,
                    symbolic_to_numeric[command.upper()],
                    prefix=prefix,
                    params=params,
                )
            if method is None:
                self.irc_unknown(prefix, command, params)

    def lineReceived(self, line: str | bytes) -> None:
        if isinstance(line, bytes):
            line = line.decode(self.settings.encoding, "replace")
        if self.debug >= 3:
            logger.debug("INCOMING LINE: %s", line)
        # lowDequote is annotated str | bytes, but str in gives str out
        line = cast(str, lowDequote(line))
        try:
            self._message_tags, untagged_line = _parse_message_tags(line)
            prefix, command, params = parsemsg(untagged_line)
            if command in numeric_to_symbolic:
                command = numeric_to_symbolic[command]
            self.handleCommand(command, prefix, params)
        except (IRCBadMessage, ValueError):
            self.badMessage(line, *exc_info())
        finally:
            self._message_tags = {}

    ###
    ### The following are "preprocessed" events normally called from IRCClient and mostly duplicated from IRCClient
    ###
    def ctcpQuery(
        self,
        user: str,
        channel: str,
        messages: Sequence[tuple[str, str | None]],
    ) -> None:
        """
        Dispatch method for any CTCP queries received.
        Duplicate tags ignored.
        Override from IRCClient
        """
        seen = set()
        nick, ident, host = processHostmask(user)
        if nick == self.nickname:
            # take note of our prefix! (for message length calculation
            self.prefixlen = len(user)
        for tag, data in messages:
            if tag not in seen:
                # dispatch event
                self.dispatch(
                    self,
                    "ctcpQuery",
                    prefix=user,
                    hostmask=user,
                    target=channel,
                    tag=tag,
                    data=data,
                    nick=nick,
                    ident=ident,
                    host=host,
                )
                # call handler if defined:
                method = getattr(self, "ctcpQuery_%s" % tag, None)
                if method is not None:
                    method(user, channel, data)
                else:
                    self.ctcpUnknownQuery(user, channel, tag, data)
            seen.add(tag)

    # borrowed mostly from IRCClient
    def ctcpQuery_VERSION(self, user: str, channel: str, data: str | None) -> None:
        if self.versionName:
            nick = user.split("!")[0]
            veritems = [self.versionName]
            if self.versionNum:
                veritems.append(self.versionNum)
            if self.versionEnv:
                veritems.append(self.versionEnv)
            self.ctcpMakeReply(nick, [("VERSION", ";".join(veritems))])

    def ctcpReply(
        self,
        user: str,
        channel: str,
        messages: Sequence[tuple[str, str | None]],
    ) -> None:
        """
        Dispatch method for any CTCP replies received.
        Duplicate tags ignored.
        Override from IRCClient
        """
        seen = set()
        nick, ident, host = processHostmask(user)
        ###
        # Commented because the prefix variable here will throw a NameError
        # and not sure how to fix
        ###
        # if nick == self.nickname:
        # take note of our prefix! (for message length calculation
        # self.prefixlen = len(prefix)
        for tag, data in messages:
            if tag not in seen:
                # dispatch event
                self.dispatch(
                    self,
                    "ctcpReply",
                    prefix=user,
                    hostmask=user,
                    target=channel,
                    tag=tag,
                    data=data,
                    nick=nick,
                    ident=ident,
                    host=host,
                )
            seen.add(tag)

    def ctcpUnknownQuery(
        self, user: str, channel: str, tag: str, data: str | None
    ) -> None:
        if self.settings.debug:
            logger.debug("Unknown CTCP query from %r: %r %r", user, tag, data)

    def signedOn(self) -> None:
        """
        Called when bot has successfully signed on to server.
        """
        logger.info("[Signed on]")

        # process nickprefixes
        # reason for this is to class prefixes in to "op" and "voice"
        # and reason for that is because most important IRC operations are classed on OP or VOICE
        self.prefixmap = PrefixMap(iter(self.supported.getFeature("PREFIX").items()))
        if self.state:
            self.state.prefixmap = self.prefixmap

        self.container._setBotinst(self)
        if self.state:
            self.state._resetnetwork()

        # allow modules to implement a delay or somesuch for joining channels if they handle this event
        if not self.dispatch(self, "preJoin"):
            for chan in self.settings.channels:
                self.join(*chan)
        self.dispatch(self, "signedOn")

    def action(self, hostmask: str, channel: str, msg: str) -> None:
        """Dispatch a CTCP ACTION as a first-class event."""
        nick, ident, host = processHostmask(hostmask)
        self.dispatch(
            self,
            "action",
            prefix=hostmask,
            hostmask=hostmask,
            target=channel,
            msg=msg,
            nick=nick,
            ident=ident,
            host=host,
            account=self._account_for(hostmask),
        )

    # overriding msg
    def msg(
        self, user: str, msg: str, length: int | None = None, strins: Any = None
    ) -> NoReturn:
        raise NotImplementedError("Use sendmsg instead.")

    # override the method that determines how a nickname is changed on
    # collisions.
    # TODO: At the moment this attempts to iterate the altnicks if it exists and falls back to
    # suffix after iterating. When to reset the iteration? At the moment it does it on connection
    # should probably make a reactor.callLater, and cancel it on disconnect or something.
    def alterCollidedNick(self, nickname: str) -> str:
        if self.settings.altnicks and self.altindex < len(self.settings.altnicks):
            s = self.settings.altnicks[self.altindex]
            self.altindex += 1
            return s
        # altnicks exhausted: grow a suffix so every attempt is a fresh nick
        # (returning settings.nick here ping-pongs between two taken nicks)
        return nickname + (self.settings.nicksuffix or "_")

    def irc_unknown(self, prefix: str, command: str, params: list[str]) -> None:
        if self.settings.debug:
            logger.debug("Unknown command: %s, %s, %s", prefix, command, params)

    ###
    ### Custom outgoing methods
    ###
    # TODO: Need to add more of these for hooking other outbound events maybe, like notice...
    def sendmsg(
        self,
        target: str,
        msg: Any,
        direct: bool = False,
        split: bool = False,
        **kwargs: Any,
    ) -> None:
        # hooks and observers always see the same event shape; when an
        # override hook is loaded it owns delivery (bypassed with direct=True)
        self.dispatch(
            self,
            "sendmsg",
            target=target,
            nick=self.nickname,
            msg=msg,
            split=split,
            **kwargs,
        )
        if self.dispatcher.sendmsg_override and not direct:
            return
        if split:
            for m in self._buildmsg(target, msg, split, **kwargs):
                self.sendLine(m)
        else:
            self.sendLine(self._buildmsg(target, msg, split, **kwargs))

    # will return true if sendmsg can proceed without truncation, false otherwise.
    # will provide incorrect results if any sendmsg hooks change lengths of messages
    # TODO: (very low priority I guess) somehow get a builtmsg from sendmsg hooks
    # NOTE: USAGE OF THIS MESSAGE MUST TEST FOR TRUE AND FALSE EXPLICITLY. None will be returned if bot isn't connected
    #         at the time of call.
    def checkSendMsg(self, target: str, msg: Any) -> bool:
        return len(
            self._buildmsg(target, msg, check=True).encode(self.settings.encoding)
        ) <= self.calcAvailableMsgLength("")

    def _buildmsg(
        self,
        target: str,
        message: Any,
        split: bool = False,
        check: bool = False,
        strins: str | list[str] | tuple[str, ...] | dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        if not isinstance(message, str):
            message = str(message)
        if strins:
            if split:
                return (
                    self.assembleMsgWLen(
                        "PRIVMSG %s :%s" % (target, msg), strins=strins, **kwargs
                    )
                    for msg in message.split("\n")
                )
            else:
                return self.assembleMsgWLen(
                    "PRIVMSG %s :%s" % (target, message), strins=strins, **kwargs
                )
        else:
            fmt = "PRIVMSG %s :%%s" % (target,)
            if split:
                msgs: list[str] = []
                for msg in message.split("\n"):
                    remaining = 4 - len(msgs)
                    if remaining <= 0:
                        break
                    for m in splitEncodedUnicode(
                        msg,
                        self.calcAvailableMsgLength(fmt % ""),
                        encoding=self.settings.encoding,
                        n=remaining,
                    ):
                        msgs.append(fmt % m[0])
                return msgs[:4]
            else:
                if check:
                    # blindly truncate message useful for checkSendMsg
                    return fmt % message
                else:
                    # auto trim message so we don't look bad when sending non interpolated message (check=false)
                    return (
                        fmt
                        % splitEncodedUnicode(
                            message,
                            self.calcAvailableMsgLength(fmt % ""),
                            encoding=self.settings.encoding,
                        )[0][0]
                    )

    # helper method to automatically truncate string to be replaced
    # TODO: need decide on string format method, either "%s" % x or "{0}".format(x)
    #     For now we are using {0} to make sure no bads with URLencoded URLs
    # TODO: this must accept either string or LIST for strins so that strins can be modified (when doing fcfs.)
    # NOTE: Calculation will be off if NL/CR or any of the "lowQuote" characters are in s or strins.
    #         You should make sure your data doesn't contain any of those characters (NL/CR/020/NUL)
    def assembleMsgWLen(
        self,
        s: str,
        strins: str | list[str] | tuple[str, ...] | dict[str, str] | None = None,
        fcfs: bool = False,
        joinsep: str | None = None,
        dropwhole: bool = False,
    ) -> str:
        enc = self.settings.encoding
        if isinstance(strins, str):
            sl = self.calcAvailableMsgLength(s.format(""))
            if sl <= 0:  # case where template string is already too big
                return splitEncodedUnicode(s, len(s) + sl, encoding=enc)[0][0]
            return s.format(splitEncodedUnicode(strins, sl, encoding=enc)[0][0])

        if strins is None:
            raise ValueError("Require list/tuple, dict, or string for strins.")
        ls = len(strins)
        if joinsep is not None:
            # lj is len(joinsep) when comparing to avail in fcfs add 2 to allow some
            # room for start of next element at least
            if isinstance(joinsep, str):
                lj = len(joinsep.encode(enc))
            else:
                lj = len(joinsep)
        if isinstance(strins, (list, tuple)):
            if joinsep is not None:
                avail = self.calcAvailableMsgLength(
                    s.format("")
                )  # must be only one replacement
            else:
                avail = self.calcAvailableMsgLength(
                    s.format(*[""] * ls)
                )  # format with empty strins to calc max avail
            if avail < 0:  # case where template string is already too big
                s = s.format(*[""] * ls)
                return splitEncodedUnicode(s, len(s) + avail, encoding=enc)[0][0]
            if dropwhole:
                # drop whole entries that don't fit rather than truncating
                # mid-entry; a later, shorter entry may still be included
                if not isinstance(strins, list):
                    strins = list(strins)
                if joinsep is None:
                    for i, replacement in enumerate(strins):
                        need = len(replacement.encode(enc))
                        if need <= avail:
                            avail -= need
                        else:
                            strins[i] = ""
                    return s.format(*strins)
                kept: list[str] = []
                for replacement in strins:
                    sep = joinsep if kept else ""
                    need = len((sep + replacement).encode(enc))
                    if need <= avail:
                        kept.append(sep + replacement)
                        avail -= need
                return s.format("".join(kept))
            if fcfs:
                # first come first served
                if not isinstance(strins, list):
                    raise ValueError("Require list/tuple, dict, or string for strins.")
                for i, replacement in enumerate(strins):
                    # get trimmed replacement and the length of that trimmed replacement
                    trimmed, trimmed_length = splitEncodedUnicode(
                        replacement, avail, encoding=enc
                    )[0]
                    # track remaining message space left
                    avail -= trimmed_length
                    # append joinsep if there's room, else make avail 0
                    if (joinsep is not None) and (i != ls - 1):
                        if avail < lj + 2:
                            avail = 0
                        else:
                            trimmed += joinsep
                            avail -= lj
                    # replace the replacement with the trimmed version
                    strins[i] = trimmed
                if joinsep is not None:
                    return s.format("".join(strins))
                else:
                    return s.format(*strins)
            else:
                # round 2, even divide
                if joinsep is not None:
                    # reserve total joinsep overhead once, then divide the rest
                    segmentlength = max(0, floor((avail - (ls - 1) * lj) / ls))
                else:
                    segmentlength = floor(avail / ls)
                if isinstance(strins, tuple):
                    strins = list(strins)
                for i, sr in enumerate(strins):
                    if (joinsep is not None) and (i != ls - 1):
                        strins[i] = (
                            splitEncodedUnicode(sr, segmentlength, encoding=enc)[0][0]
                            + joinsep
                        )
                    else:
                        strins[i] = splitEncodedUnicode(
                            sr, segmentlength, encoding=enc
                        )[0][0]
                if joinsep is not None:
                    return s.format("".join(strins))
                else:
                    return s.format(*strins)

        elif isinstance(strins, dict):
            # total space available for message
            avail = self.calcAvailableMsgLength(
                s.format(**dict(((key, "") for key in list(strins.keys()))))
            )  # format with empty strins to calc max avail
            if avail < 0:  # case where template string is already too big
                s = s.format(**dict(((key, "") for key in list(strins.keys()))))
                return splitEncodedUnicode(s, len(s) + avail, encoding=enc)[0][0]
            if fcfs:
                # first come first served (NOTE: This doesn't make much sense for an unordered thing like a dictionary)
                # hopefully we are passed an ordered dictionary or something that extends from dict.
                for key, replacement in strins.items():
                    trimmed, trimmed_length = splitEncodedUnicode(
                        replacement, avail, encoding=enc
                    )[0]
                    strins[key] = trimmed
                    avail -= trimmed_length
                return s.format(**strins)
            else:
                # round 2, even divide
                segmentlength = floor(avail / ls)
                for key, value in strins.items():
                    strins[key] = splitEncodedUnicode(
                        value, segmentlength, encoding=enc
                    )[0][0]
                return s.format(**strins)
        else:
            raise ValueError("Require list/tuple, dict, or string for strins.")

    def calcAvailableMsgLength(self, command: str) -> int:
        if self.prefixlen:
            # 510 = line terminator 508 = something else I'm not knowing about
            return (
                508
                - self.prefixlen
                - len(lowQuote(command).encode(self.settings.encoding))
            )
        else:
            return self._safeMaximumLineLength(lowQuote(command)) - 2  # line terminator

    ###
    ### Connection management methods
    ###
    def connectionMade(self) -> None:
        self._names = {}
        self._banlist = {}
        self._exceptlist = {}
        self._invitelist = {}
        self._accounts: dict[str, str] = {}
        self._message_tags = {}
        self._status_cache = {}
        self._status_pending = {}
        self._status_timeouts = {}
        self.altindex = 0
        # arm the inactivity timeout; resetTimeout in dataReceived only reschedules
        self.setTimeout(self.timeOut)
        IRCClient.connectionMade(self)
        # TODO: I think this should be on "signedOn()" just in case part of the signon is causing instant disconnect
        # reset connection factory delay:
        self.factory.resetDelay()

    def connectionLost(self, reason: Failure) -> None:  # type: ignore[override]
        self.setTimeout(None)
        IRCClient.connectionLost(self, reason)
        self._abandonLegacyStatus()
        self.container._setBotinst(None)
        if self.state:
            self.state._resetnetwork()
        # TODO: reason needs to be properly formatted/actual reason being extracted from the "Failure" or whatever
        logger.info("[disconnected: %s]", reason)
        tls_hint = _tlsConnectionErrorHint(self.settings, reason)
        if tls_hint:
            logger.warning("%s", tls_hint)


class BurlyBotFactory(ReconnectingClientFactory):
    """
    A factory for BurlyBot.
    A new protocol instance will be created each time we connect to the server.
    """

    # the class of the protocol to build when new connection is made
    protocol = BurlyBot

    def __init__(self, serversettings: Any) -> None:
        # reconnect settings
        self.container = serversettings.container
        self.maxDelay = 45
        self.factor = 1.9021605823

    def buildProtocol(self, address: Any) -> BurlyBot:
        proto = cast(BurlyBot, ReconnectingClientFactory.buildProtocol(self, address))
        proto.container = self.container
        # for shortcut access:
        proto.settings = self.container._settings
        if proto.settings.enablestate:
            proto.state = self.container.state
        else:
            proto.state = None
        proto.dispatch = proto.settings.dispatcher.dispatch
        proto.dispatcher = proto.settings.dispatcher
        proto.nickname = proto.settings.nick
        # throttle queue
        proto._dqueue = deque()
        # debug
        proto.debug = proto.settings.debug
        return proto
