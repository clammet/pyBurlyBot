from collections.abc import Callable, Iterable, Mapping
from types import ModuleType
from typing import Any, cast
from os.path import dirname, join
from os import chmod, close, execv, fdopen, fsync, open as osopen, replace, unlink
from sys import argv, executable
import sys
from tempfile import mkstemp
from copy import deepcopy
from json import dump, load, JSONEncoder
from collections import OrderedDict
from atexit import register

from twisted.internet.ssl import CertificateOptions, PrivateCertificate, platformTrust
from twisted.python import log
from twisted.internet import reactor as _reactor

# BurlyBot
from util.container import Container
from util.dispatcher import Dispatcher
from util.moduleloader import ModuleRegistry
from util.client import BurlyBotFactory
from util.db import DBManager
from util.threads import call_in_reactor
from util.timer import Timers
from util.options import Option
from util.helpers import irc_casefold

reactor: Any = _reactor

KEYS_COMMON = (
    "admins",
    "altnicks",
    "cert",
    "commandprefix",
    "datafile",
    "encoding",
    "insecure",
    "moduleopts",
    "nick",
    "nickservpass",
    "nicksuffix",
    "sasl_authzid",
    "sasl_password",
    "sasl_username",
    "verify",
)
KEYS_SERVER = (
    "serverlabel",
    *KEYS_COMMON,
    "host",
    "port",
    "channels",
    "allowmodules",
    "denymodules",
)
KEYS_SERVER_SET = set(KEYS_SERVER)
KEYS_MAIN = (
    *KEYS_COMMON,
    "console",
    "debug",
    "datadir",
    "enablestate",
    "logfile",
    "modules",
    "servers",
)
KEYS_MAIN_SET = set(KEYS_MAIN)
# expected JSON types per config key, enforced at load (#14). "modules" and
# "servers" have bespoke validation in _loadsettings.
OPTION_TYPES: dict[str, type | tuple[type, ...]] = {
    "admins": list,
    "allowmodules": (list, set),  # set is the internal round-trip form
    "altnicks": (list, str),  # a bare string is coerced to a one-element list
    "cert": str,
    "channels": list,
    "commandprefix": str,
    "console": bool,
    "datadir": str,
    "datafile": str,
    "debug": int,
    "denymodules": (list, set),  # set is the internal round-trip form
    "enablestate": bool,
    "encoding": str,
    "host": str,
    "insecure": bool,
    "logfile": str,
    "moduleopts": dict,
    "nick": str,
    "nickservpass": str,
    "nicksuffix": str,
    "port": (int, str),
    "sasl_authzid": str,
    "sasl_password": str,
    "sasl_username": str,
    "serverlabel": str,
    "verify": bool,
}
# list-typed options whose elements must all be strings
KEYS_STR_LIST = {"admins", "allowmodules", "altnicks", "denymodules"}
# keys to create a copy of so no threading bads
KEYS_COPY = {"admins", "channels", "allowmodules", "denymodules", "modules"}
# keys to deny getOption for:
# TODO: probably needs more things here
KEYS_DENY = {"_admins", "servers", "dispatcher", "moduleopts"}
# TODO: this may be incomplete
# list of module setting types to copy to make sure no thread bads
TYPE_COPY = {list, tuple, dict}

PROPERTIES_MAP = {"admins": "_admins"}

OPTION_DESC = {
    "admins": "IRC account names allowed to run administrator commands (identified nicknames on networks without IRCv3 account capabilities).",
    "altnicks": "nicknames to be tried when desired nick is in use/unavailable.",
    "encoding": "encoding to be used for sending and received messages.",
    "insecure": "allow legacy nickname-only administrator authentication.",
    "nick": "nickname to be used.",
    "nickservpass": "password to be send to nickserv on connect.",
    "nicksuffix": "suffix to be appended to nick when nick is in use/unavailable.",
    "sasl_authzid": "optional SASL authorization identity.",
    "sasl_password": "password used for the bot's SASL PLAIN login.",
    "sasl_username": "account name used for the bot's SASL PLAIN login.",
}

CORE_OPTIONS = {
    "admins": Option(list, OPTION_DESC["admins"], []),
    "insecure": Option(bool, OPTION_DESC["insecure"], False),
    "nickservpass": Option(
        str, OPTION_DESC["nickservpass"], "", secret=True, writeonly=True
    ),
    "sasl_authzid": Option(str, OPTION_DESC["sasl_authzid"], ""),
    "sasl_password": Option(
        str, OPTION_DESC["sasl_password"], "", secret=True, writeonly=True
    ),
    "sasl_username": Option(str, OPTION_DESC["sasl_username"], ""),
}

EXAMPLE_OPTS = {
    "serverlabel": "Example Server",
    "host": "irc.domain.tld",
    "channels": ["#channel1", "#channel2"],
}

EXAMPLE_OPTS2 = {
    "serverlabel": "Example Server 2",
    "host": "irc.domain.tld",
    "port": "+7001",
    "channels": ["#channel1", ["#channel2", "password"]],
}


class ConfigException(Exception):
    pass


def _validate_option(key: str, value: Any) -> None:
    expected = OPTION_TYPES.get(key)
    if expected is None:
        return
    allowed = expected if isinstance(expected, tuple) else (expected,)
    # bool subclasses int: only accept it where bool is actually expected
    ok = (bool in allowed) if isinstance(value, bool) else isinstance(value, allowed)
    if not ok:
        raise ConfigException(
            "Option %s must be %s (got %s)."
            % (key, " or ".join(t.__name__ for t in allowed), type(value).__name__)
        )
    if key in KEYS_STR_LIST and isinstance(value, (list, set)):
        if not all(isinstance(item, str) for item in value):
            raise ConfigException("Option %s must be a list of strings." % key)
    if key == "channels":
        for entry in value:
            if isinstance(entry, str):
                continue
            if (
                isinstance(entry, (list, tuple))
                and entry
                and all(isinstance(item, str) for item in entry)
            ):
                continue
            raise ConfigException(
                'channels entries must be "#chan" or ["#chan", "key"].'
            )


class NoDefault:
    pass


# This is managed from dispatcher, but accessed is managed through settings, and called from container.
# (container wrapped the call in callfromthread if needed and settings managed allowed access)
class _ADDONS:
    def __init__(self) -> None:
        self._dict: dict[str, tuple[str, Callable[..., Any]]] = {}

    def clear(self) -> None:
        self._dict.clear()

    def _add(self, addonname: str, modulename: str, f: Callable[..., Any]) -> None:
        self._dict[addonname] = (modulename, f)

    def _getModuleAddon(self, addonname: str) -> tuple[str, Callable[..., Any]]:
        return self._dict[addonname]


class BaseServer:
    moduleopts: dict[str, dict[str, Any]]
    serverlabel: str
    host: str
    port: int
    ssl: bool
    channels: list[tuple[str, ...]]
    allowmodules: set[str]
    denymodules: set[str]

    def __init__(self, opts: Mapping[str, Any]) -> None:
        self.moduleopts = {}
        self.setup(opts)

    # special handler for .admins (.lowers() each nick on set to make for easier checking in wrapper.isadmin)
    @property
    def admins(self) -> list[str]:
        return self.__dict__.get("_admins", Settings._admins)

    @admins.setter
    def admins(self, value: Iterable[str]) -> None:
        self._admins = [x.casefold() for x in value]

    def is_admin(
        self, nick: str | None, account: str | None, admins: Iterable[str] | None = None
    ) -> bool:
        allowed = {
            item.casefold() for item in (admins if admins is not None else self.admins)
        }
        if account and account != "*" and account.casefold() in allowed:
            return True
        return bool(
            getattr(self, "insecure", False)
            and nick
            and irc_casefold(nick) in {irc_casefold(item) for item in allowed}
        )

    def warn_insecure_auth(self) -> None:
        if getattr(self, "insecure", False):
            print(
                "WARNING: insecure nickname-only administrator authentication is active "
                "for %s. Nicknames are not identities and can be impersonated."
                % self.serverlabel,
                file=sys.stderr,
            )
        if self.ssl and not getattr(self, "verify", True):
            print(
                "WARNING: TLS certificate verification is disabled for %s. "
                "The IRC server identity is not authenticated." % self.serverlabel,
                file=sys.stderr,
            )

    def setup(self, opts: Mapping[str, Any]) -> None:
        if bool(opts.get("sasl_username")) != bool(opts.get("sasl_password")):
            raise ConfigException(
                "sasl_username and sasl_password must be configured together."
            )
        # A reload must not retain options that were removed or changed to a false value.
        for key in KEYS_SERVER:
            self.__dict__.pop(PROPERTIES_MAP.get(key, key), None)
        # must stay a per-server dict; without this, attribute fallback would
        # resolve moduleopts to the global Settings.moduleopts dict
        self.moduleopts = {}
        self.channels = []
        self.allowmodules = set()
        self.denymodules = set()
        for key in KEYS_SERVER:
            opt = opts.get(key, None)
            if opt is not None:
                _validate_option(key, opt)
            if key == "serverlabel":
                if opt is None:
                    raise ConfigException("Missing serverlabel.")
                elif ":" in opt:
                    raise ConfigException('serverlabel (%s) cannot contain ":"' % opt)
            elif key == "host" and opt is None:
                raise ConfigException("%s must have a host" % self.serverlabel)

            if key == "altnicks":
                if opt is not None:
                    self.altnicks = (
                        list(opt) if isinstance(opt, (list, tuple)) else [opt]
                    )
            elif key == "port":
                # process port number with SSL prefix
                # TODO: should we have a server config attribute called "ssl" instead?
                opt = opt if opt is not None else "6667"
                if isinstance(opt, int):
                    self.ssl = False
                elif opt.startswith("+"):
                    opt = opt[1:]
                    self.ssl = True
                else:
                    self.ssl = False
                self.port = int(opt)
            elif key == "channels":
                if opt:
                    for channel in opt:
                        if isinstance(channel, list):
                            if len(channel) > 1 and channel[1]:
                                self.channels.append((channel[0], channel[1]))
                            else:
                                self.channels.append((channel[0],))
                        else:
                            self.channels.append((channel,))

            elif key == "allowmodules":
                self.allowmodules = set(opt) if opt else set()
            elif key == "denymodules":
                self.denymodules = set(opt) if opt else set()
            elif opt is not None:
                setattr(self, key, opt)
        if opts.get("sasl_username") and not self.ssl:
            raise ConfigException("SASL PLAIN credentials require a TLS server port.")

    def _getDict(self) -> OrderedDict[str, Any]:
        d: OrderedDict[str, Any] = OrderedDict()
        for option_name in KEYS_SERVER:
            attribute_name = PROPERTIES_MAP.get(option_name, option_name)
            if attribute_name in self.__dict__:
                value = self.__dict__[attribute_name]  # bypass __getattr__ override
                if value is not None:
                    # preprocess channels
                    if option_name == "channels":
                        channels: list[Any] = []
                        for channel in value:
                            if len(channel) == 1:
                                channels.append(channel[0])
                            else:
                                channels.append(channel)
                        d[option_name] = channels
                    elif option_name == "port":
                        d[option_name] = (
                            value if not self.__dict__["ssl"] else "+" + str(value)
                        )
                    else:
                        d[option_name] = value
        return d


DummyServer = BaseServer  # alias to make example server code clear


class Server(BaseServer):
    def __init__(self, opts: Mapping[str, Any]) -> None:
        super().__init__(opts)
        self.addons: _ADDONS | None = None
        # dispatcher placeholder (probably not needed)
        self.dispatcher: Dispatcher | None = None
        # TODO: fix the complicated relationship between Factory<->Settings<->Container
        #       also the relationship between Dispatcher<->Settings<->Dispatcher
        self.container = Container(self)
        self._factory = BurlyBotFactory(self)

    def reload_modules(self, registry: ModuleRegistry) -> None:
        # Addons should only be created once
        if self.addons is None:
            self.addons = _ADDONS()
        else:
            self.addons.clear()
        # Assign the dispatcher before loading so module init() can resolve dependencies.
        if self.dispatcher is None:
            self.dispatcher = Dispatcher(self, registry)
        else:
            self.dispatcher.registry = registry
        self.dispatcher.reload()
        # Reload tears down module state (timers etc.) that modules only re-arm
        # on signedon; when this server is already connected that event will
        # never fire again, so post a synthetic one (#74). postEvent dispatches
        # on a later reactor turn, i.e. after loading completes.
        if self.container._botinst is not None:
            self.container.postEvent("signedon")

    def reload_current_modules(self) -> None:
        self.reload_modules(Settings.module_registry)

    def __getattr__(self, name: str) -> Any:
        # only called when instance lookup fails: fall back to global Settings
        return getattr(Settings, name)

    def getOptions(self, opts: Iterable[str], **kwargs: Any) -> list[Any]:
        vals = []
        for opt in opts:
            vals.append(self.getOption(opt, **kwargs))
        return vals

    @staticmethod
    def _copyOnReturn(value: Any, force: bool = False) -> Any:
        if force or type(value) in TYPE_COPY:  # copy value if compound datatype
            # copy in the reactor thread so a concurrent setOption can't be
            # observed half-applied
            return call_in_reactor(deepcopy, value)
        return value

    @staticmethod
    def _lookupModuleOpt(
        moduleopts: Mapping[str, dict[str, Any]],
        module: str,
        opt: str,
        channel: str | bool | None,
    ) -> tuple[bool, Any]:
        if module in moduleopts:
            mod = moduleopts[module]
            if (
                channel
                and "_channels" in mod
                and channel in mod["_channels"]
                and opt in mod["_channels"][channel]
            ):
                return True, mod["_channels"][channel][opt]
            if opt in mod:
                return True, mod[opt]
        return False, None

    def _resolveServerModuleOpts(
        self, server: str | bool | None
    ) -> dict[str, dict[str, Any]]:
        if server is not None:
            try:
                return Settings.servers[cast(str, server)].moduleopts
            except KeyError:
                raise ValueError("Server (%s) not found" % server) from None
        return self.moduleopts

    @staticmethod
    def _globalModuleOpts() -> dict[str, dict[str, Any]]:
        global_moduleopts = Settings.moduleopts
        if global_moduleopts is None:
            raise RuntimeError("Global module options are not initialized.")
        return global_moduleopts

    def _resolveServerObj(self, server: str | bool | None) -> Any:
        if server is None:
            return self
        if server:
            if server not in Settings.servers:
                raise ValueError("Server label (%s) not found." % server)
            return Settings.servers[server]
        return server

    # if channel or server is set, retrieve for that specific thing.
    # if channel or server is False, retrieve "global" for that thing.
    # TODO: make sure this optimized as it can be
    def getOption(
        self,
        opt: str,
        module: str | None = None,
        channel: str | bool | None = None,
        server: str | bool | None = None,
        default: Any = NoDefault,
        setDefault: bool = True,
    ) -> Any:
        if opt in KEYS_DENY:
            raise ValueError("Access denied. (%s)" % opt)
        if module:
            if server or server is None:
                # try searching for option in a server object
                moduleopts = self._resolveServerModuleOpts(server)
                found, value = self._lookupModuleOpt(moduleopts, module, opt, channel)
                if found:
                    return self._copyOnReturn(value)
            # fall back to global moduleopts (or server was False)
            global_moduleopts = self._globalModuleOpts()
            found, value = self._lookupModuleOpt(
                global_moduleopts, module, opt, channel
            )
            if found:
                return self._copyOnReturn(value)
            if default is NoDefault:
                raise AttributeError("No setting (%s) for module: %s" % (opt, module))
            else:
                if setDefault:
                    global_moduleopts.setdefault(module, {})[opt] = default
                return default
        # non-module (core) options
        if channel:
            raise ValueError("Core option (%s) cannot be channel-scoped." % opt)
        server_obj = self._resolveServerObj(server)

        if server_obj and opt in KEYS_SERVER_SET:
            value = getattr(server_obj, opt)
        else:
            if not server_obj or server_obj is self:
                if opt not in KEYS_MAIN_SET:
                    raise ValueError("Settings has no option: (%s) to get." % opt)
                else:
                    value = getattr(Settings, opt)
            else:
                # case where a server setting is specifically attempted to be got, but it's not in KEYS_SERVER
                # instead of falling back to KEYS_MAIN, raise error
                raise ValueError("Server setting has no option: (%s) to get." % opt)
        if opt in KEYS_COPY:  # always copy: these are compound datatypes
            return self._copyOnReturn(value, force=True)
        else:
            return value

    def setOption(
        self,
        opt: str,
        value: Any,
        module: str | None = None,
        channel: str | bool | None = None,
        server: str | bool | None = None,
    ) -> None:
        if opt in KEYS_DENY:
            raise ValueError("Access denied. (%s)" % opt)
        if type(value) in TYPE_COPY:
            value = deepcopy(value)  # copy value if compound datatype

        if module:
            if server or server is None:
                # target a server's module options
                moduleopts = self._resolveServerModuleOpts(server)
            else:
                # if server was False, (setting "global")
                moduleopts = self._globalModuleOpts()
            mod = moduleopts.setdefault(module, {})
            if channel:
                mod.setdefault("_channels", {}).setdefault(channel, {})[opt] = value
            else:
                mod[opt] = value
        else:
            if channel:
                raise ValueError("Core option (%s) cannot be channel-scoped." % opt)
            server_obj = self._resolveServerObj(server)

            if server_obj and opt in KEYS_SERVER_SET:
                setattr(server_obj, opt, value)
            else:
                if not server_obj or server_obj is self:
                    if opt not in KEYS_MAIN_SET:
                        raise ValueError("Settings has no option: (%s) to set." % opt)
                    else:
                        setattr(Settings, opt, value)
                else:
                    # case where a server setting is specifically attempted to be set, but it's not in KEYS_SERVER
                    # instead of falling back to KEYS_MAIN, raise error
                    raise ValueError(
                        "Server settings has no option: (%s) to set." % opt
                    )

    def getModule(self, modname: str) -> ModuleType:
        if not self.isModuleAvailable(modname):
            raise ConfigException("Module (%s) is not available." % modname)
        if self.dispatcher is None:
            raise ConfigException("Module dispatcher has not been initialized.")
        module = self.dispatcher.get_module(modname)
        if module is None:
            raise ConfigException("Module (%s) is not active." % modname)
        return module

    def isModuleAvailable(self, modname: str) -> bool:
        return self.dispatcher is not None and self.dispatcher.is_module_loaded(modname)

    def getAddon(self, addonname: str) -> Callable[..., Any]:
        if self.addons is None:
            raise ConfigException("Module addons have not been initialized.")
        try:
            modname, f = self.addons._getModuleAddon(addonname)
        except KeyError:
            raise AttributeError("No provider for %s" % addonname) from None
        if self.isModuleAvailable(modname):
            return f
        else:
            raise AttributeError(
                "Provider %s is not available because module (%s) is not available."
                % (addonname, modname)
            )


class SettingsBase:
    nick: str = "BurlyBot"
    altnicks: list[str] | tuple[()] = ()
    nicksuffix: str = "_"
    nickservpass: str | None = None
    sasl_username: str | None = None
    sasl_password: str | None = None
    sasl_authzid: str | None = None
    insecure: bool = False
    commandprefix: str = "!"
    datadir: str = "data"
    debug: int = 0
    datafile: str = "BurlyBot.db"
    enablestate: bool = False
    encoding: str = "utf-8"
    cert: str | None = None
    verify: bool = True
    console: bool = True
    logfile: str | None = None
    modules: list[str] | tuple[()] = ()
    _admins: list[str] | tuple[()] = ()
    botdir: str | None = None
    configfile: str | None = None
    moduleopts: dict[str, dict[str, Any]] | None = None
    databasemanager: DBManager | None = None

    @property
    def admins(self) -> list[str]:
        return list(self._admins)

    @admins.setter
    def admins(self, value: Iterable[str]) -> None:
        self._admins = [x.casefold() for x in value]

    # TODO: not sure if the following is needed or not. Class.dict seems to behave strangely
    def _setDefaults(self) -> None:
        self.altnicks = []
        self.modules = []
        self._admins = []
        self.moduleopts = {}

    def __init__(self) -> None:
        self.servers: dict[str, Server] = {}
        self.newservers: list[Server] = []
        self.oldservers: set[str] = set()
        self.module_registry = ModuleRegistry()
        self._setDefaults()

    def _loadsettings(self) -> None:
        if self.configfile is None:
            raise ConfigException("No configuration file specified.")
        try:
            with open(self.configfile, encoding="utf-8") as config_file:
                newsets = load(config_file)
        except (OSError, ValueError) as e:
            raise ConfigException(
                "Config file (%s) contains errors: %s"
                "\nTry http://jsonlint.com/ and make sure no trailing commas."
                % (self.configfile, e)
            ) from e
        if not isinstance(newsets, dict):
            raise ConfigException("Config file root must be a JSON object.")
        if "servers" in newsets and not isinstance(newsets["servers"], list):
            raise ConfigException("The servers option must be a JSON array.")

        # Stage the complete configuration first. No live setting or server is
        # changed until every entry has passed validation.
        candidate = SettingsBase()
        for option_name in KEYS_MAIN:
            if option_name == "servers" or option_name not in newsets:
                continue
            value = newsets[option_name]
            if option_name == "modules":
                if not isinstance(value, list) or not all(
                    isinstance(module_name, str) for module_name in value
                ):
                    raise ConfigException(
                        "The modules option must be an array of names."
                    )
                value = list(dict.fromkeys(value))
            else:
                _validate_option(option_name, value)
            setattr(candidate, option_name, value)
        if bool(candidate.sasl_username) != bool(candidate.sasl_password):
            raise ConfigException(
                "Global sasl_username and sasl_password must be configured together."
            )

        validated_servers: list[tuple[str, BaseServer]] = []
        if "servers" in newsets:
            labels: set[str] = set()
            for position, server_options in enumerate(newsets["servers"], start=1):
                if not isinstance(server_options, dict):
                    raise ConfigException(
                        "Server configuration %d must be an object." % position
                    )
                label = server_options.get("serverlabel")
                if not isinstance(label, str) or not label:
                    raise ConfigException(
                        "Server configuration %d has no valid serverlabel." % position
                    )
                if label in labels:
                    raise ConfigException("Duplicate serverlabel: %s" % label)
                labels.add(label)
                try:
                    validated = BaseServer(server_options)
                except (TypeError, ValueError) as exc:
                    raise ConfigException(
                        "Invalid server configuration for %s: %s" % (label, exc)
                    ) from exc
                effective_username = server_options.get(
                    "sasl_username", candidate.sasl_username
                )
                effective_password = server_options.get(
                    "sasl_password", candidate.sasl_password
                )
                if bool(effective_username) != bool(effective_password):
                    raise ConfigException(
                        "Server %s must configure sasl_username and sasl_password together."
                        % label
                    )
                if effective_username and not validated.ssl:
                    raise ConfigException(
                        "Server %s must use TLS when SASL PLAIN is configured." % label
                    )
                validated_servers.append((label, validated))

        for option_name in KEYS_MAIN:
            if option_name != "servers":
                setattr(self, option_name, deepcopy(getattr(candidate, option_name)))

        newservers: list[Server] = []
        oldservers: set[str] = set()
        if "servers" in newsets:
            oldservers = set(self.servers)
            for label, validated in validated_servers:
                options = validated._getDict()
                if label in self.servers:
                    self.servers[label].setup(options)
                else:
                    server = Server(options)
                    self.servers[label] = server
                    newservers.append(server)
                oldservers.discard(label)
        self.newservers = newservers
        self.oldservers = oldservers

    def _connect(self, servers: Iterable[Server]) -> None:
        manager = self.databasemanager
        if manager is None:
            raise RuntimeError("Database manager has not been initialized.")
        for server in servers:
            if server.ssl:
                try:
                    reactor.connectSSL(
                        server.host,
                        server.port,
                        server._factory,
                        createCertOptions(server),
                    )
                except Exception as e:  # noqa: BLE001 - Twisted connector boundary
                    print(
                        "SSL Error: Cannot connect to '%s' (%s)"
                        % (server.serverlabel, e)
                    )
                    # fully retire the server so it is not left half-registered
                    # (in Settings.servers with a live factory but no database)
                    server._factory.stopTrying()
                    manager.delServer(server.serverlabel)
                    self.servers.pop(server.serverlabel, None)
            else:
                reactor.connectTCP(server.host, server.port, server._factory)

    def createDatabases(self, servers: Iterable[Server]) -> None:
        manager = self.databasemanager
        if manager is None:
            raise RuntimeError("Database manager has not been initialized.")
        for server in servers:
            manager.addServer(server.serverlabel, server.datafile)

    def _disconnect(self, servers: Iterable[str]) -> None:
        manager = self.databasemanager
        if manager is None:
            raise RuntimeError("Database manager has not been initialized.")
        # NOTE: this is serverlabel
        for server_label in servers:
            print("DISCONNECTING: %s" % server_label)
            server = self.servers[server_label]
            if server.container._botinst:
                server.container._botinst.quit()
            server._factory.stopTrying()
            # callLater delserver so that just incase some modules catch quit or error event, and use DB for it
            # May cause race condition when connecting to new server that uses same name but different DBfile
            # Hope someone doesn't do that...
            reactor.callLater(1.0, manager.delServer, server.serverlabel)
            # remove oldservers from servers dict
            try:
                del self.servers[server.serverlabel]
            except KeyError:
                print("Warning: tried to remove server that didn't exist")

    def load(self) -> None:
        self.reloadStage1()

    def reloadStage1(self) -> None:
        if self.configfile:
            self._loadsettings()
        else:
            for option_name in KEYS_MAIN:
                # skip properties: their backing state is reset by _setDefaults,
                # and class-level getattr would return the descriptor itself
                if option_name != "servers" and option_name not in PROPERTIES_MAP:
                    setattr(self, option_name, getattr(SettingsBase, option_name))
            self._setDefaults()

    def reloadStage2(self) -> None:
        # disconnect before reloading dispatchers
        self._disconnect(self.oldservers)
        # create databases so init() can do database things.
        self.createDatabases(self.newservers)
        self.module_registry.reload_servers(self.servers.values())
        for server in self.servers.values():
            server.warn_insecure_auth()
        # connect after load dispatchers
        self._connect(self.newservers)
        self.oldservers = set()
        self.newservers = []

    # TODO: when twisted supports good logger, consider allowing per-server logfile
    # NOTE: logfile is not chat logging
    # This must be called only once
    def initialize(self, logger: Any = None) -> None:
        # setup log options
        if not self.console:
            logger.stop()
        if self.logfile:
            if self.botdir is None:
                raise RuntimeError("Bot directory has not been configured.")
            log.startLogging(
                open(join(self.botdir, self.logfile), "a"), setStdout=False
            )

        # setup global database and databasemanager
        self.databasemanager = DBManager(self.datadir, self.datafile)
        self.reloadStage2()
        # start dbcommittimer
        # TODO: figure out if actually need this, and what SQLite transaction/journaling mode we should be using
        Timers._addTimer(
            "_dbcommit", 60 * 60, self.databasemanager.dbcommit, reps=-1
        )  # every hour (60*60)

    def saveOptions(self) -> None:
        d = OrderedDict()
        for key in KEYS_MAIN:
            if key == "servers":
                continue
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        if self.servers:
            d["servers"] = [serv._getDict() for serv in self.servers.values()]
        else:
            EXAMPLE_SERVER = DummyServer(EXAMPLE_OPTS)
            EXAMPLE_SERVER2 = DummyServer(EXAMPLE_OPTS2)
            d["servers"] = [EXAMPLE_SERVER._getDict(), EXAMPLE_SERVER2._getDict()]
        if self.configfile is None:
            raise ConfigException("No configuration file specified.")
        config_dir = dirname(self.configfile) or "."
        fd, temporary = mkstemp(prefix=".BurlyBot-", suffix=".json.tmp", dir=config_dir)
        try:
            chmod(temporary, 0o600)
            with fdopen(fd, "w", encoding="utf-8") as config_file:
                dump(
                    d, config_file, indent=4, separators=(",", ": "), cls=ConfigEncoder
                )
                config_file.write("\n")
                config_file.flush()
                fsync(config_file.fileno())
            replace(temporary, self.configfile)
            try:
                directory_fd = osopen(config_dir, 0)
                try:
                    fsync(directory_fd)
                finally:
                    close(directory_fd)
            except OSError:
                # Some platforms/filesystems do not permit syncing directory handles.
                pass
        finally:
            try:
                unlink(temporary)
            except FileNotFoundError:
                pass

    def shutdown(self, relaunch: bool = False) -> None:
        manager = self.databasemanager
        if manager is None:
            raise RuntimeError("Database manager has not been initialized.")
        self._disconnect(list(self.servers.keys()))
        # stop timers or just not care...
        Timers._stopall()
        reactor.callLater(2.0, manager.shutdown)  # to give time for individual shutdown
        self.module_registry.unload()
        reactor.callLater(2.5, reactor.stop)  # to give time for individual shutdown
        # TODO: make sure this works properly
        #     it may act odd on Windows due to execv not replacing current process.
        if relaunch:
            register(relaunchfunc, executable, argv)

    def hardshutdown(self) -> None:
        manager = self.databasemanager
        if manager is None:
            raise RuntimeError("Database manager has not been initialized.")
        Timers._stopall()
        self.module_registry.unload()
        manager.shutdown()


def relaunchfunc(pythonbin: str, args: list[str]) -> None:
    args.insert(0, pythonbin)
    execv(pythonbin, args)  # noqa: S606 - explicit configured interpreter and argv


class ConfigEncoder(JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, set):
            return list(obj)
        return JSONEncoder.default(self, obj)


def createCertOptions(server: Server) -> CertificateOptions:
    pk = None
    cert = None
    if server.cert:
        with open(server.cert, "rb") as cert_file:
            pc = PrivateCertificate.loadPEM(cert_file.read())
        pk = pc.privateKey.original
        cert = pc.original
    tr = platformTrust() if server.verify else None
    return CertificateOptions(privateKey=pk, certificate=cert, trustRoot=tr)


Settings = SettingsBase()
