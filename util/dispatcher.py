from copy import copy
from types import ModuleType
from typing import Any, cast
from twisted.internet.threads import deferToThread

from traceback import format_exc
from operator import attrgetter
from functools import partial

from .wrapper import BotWrapper
from .container import Container, SetupContainer, WaitData
from .helpers import commandSplit, coerceToUnicode
from .event import Event
from .moduleloader import ModuleLoadError
from .moduleloader import ModuleRegistry
from .mapping import Mapping, MappingFunction
from .types import BotLike
from .options import option_spec


class Dispatcher:
    def __init__(self, settings: Any, registry: ModuleRegistry) -> None:
        self.waitmap: dict[str | None, set[WaitData]] = {}
        self.eventmap: dict[str, dict[str, Any]] = {}
        self.settings = settings
        self.registry = registry
        self.serverlabel = settings.serverlabel
        self.debug = settings.debug

    def reload(self) -> None:
        self.registry.clear_server(self.serverlabel)
        self.eventmap = {}
        # don't clear waitmap on reload to allow for still waiting functions to pass
        # it's also self managed (hopefully)
        self.MSGHOOKS = False

        settings = self.settings
        configured = settings.allowmodules or settings.modules
        self.allowed_modules = set(configured) - set(settings.denymodules)
        load_order = sorted(configured) if isinstance(configured, set) else configured
        for module_name in load_order:
            if module_name in self.allowed_modules:
                self.load_module(module_name)
        self._build_event_map()

    @property
    def modules(self) -> dict[str, ModuleType]:
        return self.registry.active_modules(self.serverlabel)

    def get_module(self, name: str) -> ModuleType | None:
        return self.modules.get(name)

    def is_module_loaded(self, name: str) -> bool:
        return name in self.modules

    @staticmethod
    def _requirements(module: ModuleType) -> tuple[str, ...]:
        requirements = getattr(module, "REQUIRES", ())
        if requirements is None:
            return ()
        if isinstance(requirements, str):
            return (requirements,)
        return tuple(requirements)

    def _load_requirements(
        self, module: ModuleType, parents: tuple[str, ...]
    ) -> tuple[set[str], set[str]]:
        notallowed = set()
        failed = set()
        for requirement in self._requirements(module):
            if requirement in self.modules:
                continue
            if requirement not in self.allowed_modules:
                notallowed.add(requirement)
                continue
            if not self.load_module(requirement, parents):
                failed.add(requirement)
        return notallowed, failed

    def load_module(self, name: str, parents: tuple[str, ...] = ()) -> bool:
        if name in self.modules:
            return True
        if name not in self.allowed_modules:
            self.registry.record_activation_error(
                self.serverlabel, name, "Module is not allowed."
            )
            return False
        if name in parents:
            cycle = " -> ".join((*parents, name))
            self.registry.record_activation_error(
                self.serverlabel, name, "Circular module dependency: %s" % cycle
            )
            return False

        print("Loading %s..." % name)
        module = self.registry.import_plugin(name)
        if module is None:
            return False

        notallowed, failed = self._load_requirements(module, (*parents, name))
        if notallowed or failed:
            parts = []
            if notallowed:
                parts.append("not allowed: %s" % ", ".join(sorted(notallowed)))
            if failed:
                parts.append("failed: %s" % ", ".join(sorted(failed)))
            self.registry.record_activation_error(
                self.serverlabel,
                name,
                "Requirements could not be loaded (%s)." % "; ".join(parts),
            )
            return False

        stages = (
            ("OPTIONS", self._configure_module),
            ("init()", self._initialize_module),
            ("PROVIDES", self._register_addons),
        )
        for stage_name, stage in stages:
            try:
                stage(name, module)
            except ModuleLoadError as exc:
                self.registry.record_activation_error(self.serverlabel, name, str(exc))
                return False
            except Exception:  # noqa: BLE001 - third-party module stage boundary
                self.registry.record_activation_error(
                    self.serverlabel,
                    name,
                    "Error in %s:\n%s" % (stage_name, format_exc()),
                )
                return False

        self.registry.activate(self.serverlabel, name, module)
        print("Loaded %s." % name)
        return True

    def _configure_module(self, name: str, module: ModuleType) -> None:
        for option, params in getattr(module, "OPTIONS", {}).items():
            try:
                spec = option_spec(params)
            except (TypeError, ValueError) as exc:
                raise ModuleLoadError(
                    "Invalid OPTIONS entry %r: %s" % (option, exc)
                ) from exc
            self.settings.getOption(
                option,
                server=False,
                module=name,
                default=spec.default,
                setDefault=True,
                inreactor=True,
            )

    def _initialize_module(self, name: str, module: ModuleType) -> None:
        initialize = getattr(module, "init", None)
        if initialize is not None and not initialize(
            SetupContainer(self.settings.container)
        ):
            raise ModuleLoadError("init() returned a false value.")

    def _register_addons(self, name: str, module: ModuleType) -> None:
        provided = {
            item: getattr(module, item) for item in getattr(module, "PROVIDES", ())
        }
        for item, value in provided.items():
            self.settings.addons._add(item, name, value)

    def _build_event_map(self) -> None:
        for module in self.modules.values():
            self._add_mappings(module)
        for event_mappings in self.eventmap.values():
            event_mappings["instant"].sort(key=attrgetter("priority"))
            event_mappings["regex"].sort(key=attrgetter("priority"))
            for command_mappings in event_mappings["command"].values():
                command_mappings.sort(key=attrgetter("priority"))

    def _add_mappings(self, module: ModuleType) -> None:
        eventmap = self.eventmap
        mapping_factory = getattr(module, "get_mappings", None)
        if mapping_factory is not None:
            mappings = mapping_factory(SetupContainer(self.settings.container))
        else:
            mappings = getattr(module, "mappings", ())
        for mapping in mappings:
            for raw_event_type in mapping.types:
                event_type = raw_event_type.lower()

                event_mappings = eventmap.setdefault(
                    event_type, {"instant": [], "regex": [], "command": {}}
                )

                if event_type == "sendmsg" and mapping.override:
                    self.MSGHOOKS = True
                if not mapping.command and not mapping.regex:
                    event_mappings["instant"].append(mapping)

                if mapping.command:
                    mapcom = mapping.command
                    # Mapping normalizes a single command string into an iterable.
                    if mapping.function and isinstance(mapping.function, partial):
                        # little cheat for adding module to functools.partial things like in simplecommands
                        mapping.function.__module__ = module.__name__
                    for raw_command_name in mapcom:
                        command_name = raw_command_name.lower()
                        event_mappings["command"].setdefault(command_name, []).append(
                            mapping
                        )

                if mapping.regex:
                    event_mappings["regex"].append(mapping)

    def _getCommandMappings(
        self, cmd: str | None = None
    ) -> list[Mapping] | list[list[Mapping]]:
        if cmd:
            return self.eventmap.get("privmsged", {}).get("command", {}).get(cmd, [])
        else:
            return list(self.eventmap.get("privmsged", {}).get("command", {}).values())

    def isAdminCommand(self, event_type: str, msg: str | None) -> bool:
        """Cheaply check whether ``msg`` invokes an admin-only command for ``event_type``."""
        prefix = self.settings.commandprefix
        if not msg or not prefix or not msg.startswith(prefix):
            return False
        parsed_command, _ = commandSplit(msg)
        if parsed_command is None:
            return False
        command = parsed_command[len(prefix) :].lower()
        mappings = (
            self.eventmap.get(event_type.lower(), {})
            .get("command", {})
            .get(command, ())
        )
        return any(mapping.admin for mapping in mappings)

    def dispatch(self, botinst: Any, event_type: str, **eventkwargs: Any) -> bool:
        settings = self.settings
        cont_or_wrap = botinst.container
        event = None
        # Case insensitivity for event_type lookups
        l_event_type = event_type.lower()
        dispatched = False

        msg = eventkwargs.get("msg", None)
        if msg and l_event_type != "sendmsg":
            eventkwargs["msg"] = msg = coerceToUnicode(
                eventkwargs["msg"], settings.encoding
            )
        command = ""
        if l_event_type != "sendmsg" and msg and msg.startswith(settings.commandprefix):
            # case insensitive command (see below)
            # commands can't have spaces in them, and lol command prefix can't be a space
            # if you want a case sensitive match you can do your command as a regex
            parsed_command, argument = commandSplit(msg)
            if parsed_command is None:
                return False
            # support multiple character commandprefix
            command = parsed_command[len(settings.commandprefix) :]
            # Maintain case for event, for funny things like replying in all caps
            eventkwargs["command"], eventkwargs["argument"] = (command, argument)
            command = command.lower()
        eventmap = self.eventmap
        command_mappings = (
            eventmap.get(l_event_type, {}).get("command", {}).get(command, ())
        )
        eventkwargs["is_command"] = bool(command_mappings)
        eventkwargs["is_admin_command"] = any(
            mapping.admin for mapping in command_mappings
        )
        if l_event_type in eventmap:
            # TODO: Event and wrapper creation could possible be delayed even further maybe
            if event is None:
                eventkwargs["encoding"] = settings.encoding
                event, cont_or_wrap = self.createEventAndWrap(
                    cont_or_wrap, l_event_type, eventkwargs
                )
            if self.debug >= 2:
                print("DISPATCHING: %s" % event)
            # priority 0 is a total override: no further handlers run for this event
            overridden = False
            # lol dispatcher is 100 more simple now, but at the cost of more dict...
            for mapping in eventmap[l_event_type]["instant"]:
                self._dispatchreally(mapping.function, event, cont_or_wrap, self.debug)
                dispatched = True
                if mapping.priority == 0:
                    overridden = True
                    break
            # super fast command dispatching now... Only thing left that's slow is the regex but has to be
            if not overridden:
                for mapping in command_mappings:
                    if mapping.admin and not settings.is_admin(
                        event.nick, event.account
                    ):
                        # TODO: Do we bot.say("access denied") ?
                        continue
                    self._dispatchreally(
                        mapping.function, event, cont_or_wrap, self.debug
                    )
                    dispatched = True
                    if mapping.priority == 0:
                        overridden = True
                        break
            if not overridden:
                for mapping in eventmap[l_event_type]["regex"]:
                    if msg is None:
                        continue
                    result = mapping.regex.search(msg)
                    if result:
                        # handlers run in threads: give each its own event so a
                        # later match can't overwrite regex_match mid-handler
                        regex_event = copy(event)
                        regex_event.regex_match = result
                        self._dispatchreally(
                            mapping.function, regex_event, cont_or_wrap, self.debug
                        )
                        dispatched = True
                        if mapping.priority == 0:
                            break

        if l_event_type in self.waitmap:
            # special map to deal with WaitData
            # delayed event creation as late as possible:
            if event is None:
                event, cont_or_wrap = self.createEventAndWrap(
                    cont_or_wrap, l_event_type, eventkwargs
                )
            wdset = self.waitmap[l_event_type]
            remove = []
            for wd in wdset:
                # if found stopevent add it to list to remove after iteration
                if l_event_type in wd.stope:
                    remove.append(wd)
                    wd.q.put(event)
                    wd.done = True
                    dispatched = True
                elif l_event_type in wd.interestede:
                    wd.q.put(event)
                    dispatched = True
            if remove:
                for x in remove:
                    self.delWaitData(x)
        return dispatched

    @staticmethod
    def createEventAndWrap(
        cont_or_wrap: Container | BotWrapper,
        eventtype: str,
        eventkwargs: dict[str, Any],
    ) -> tuple[Event, Container | BotWrapper]:
        event = Event(eventtype, **eventkwargs)
        if event.target or event.nick:
            return event, BotWrapper(event, cast(Container, cont_or_wrap))
        else:
            return event, cont_or_wrap

    @staticmethod
    def _dispatchreally(
        func: MappingFunction,
        event: Event,
        cont_or_wrap: Container | BotWrapper,
        debug: int,
    ) -> None:
        if debug >= 2:
            print("DISPATCHING TO: %r" % func)
        # Module handlers may perform blocking HTTP or parsing. Keep every mapped
        # callback behind the same worker boundary so the reactor remains responsive.
        d = deferToThread(func, event, cast(BotLike, cont_or_wrap))
        # add errback
        d.addErrback(cont_or_wrap._moduleerr)

    def addWaitData(self, wd: WaitData) -> None:
        for ietype in wd.interestede:
            self.waitmap.setdefault(ietype, set()).add(wd)
        for setype in wd.stope:
            self.waitmap.setdefault(setype, set()).add(wd)

    def delWaitData(self, wd: WaitData) -> None:
        for wdtype in (wd.interestede, wd.stope):
            for etype in wdtype:
                wdset = self.waitmap.get(etype)
                if wdset:
                    try:
                        wdset.remove(wd)
                    except KeyError:
                        pass
                    if not wdset:
                        self.waitmap.pop(etype, None)
