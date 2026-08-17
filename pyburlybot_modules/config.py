from util.event import Event
from util.types import BotLike

from util import Mapping, argumentSplit, functionHelp
from util.options import Option, option_spec
from util.settings import CORE_OPTIONS, Settings
from util.threads import call_in_reactor


from json import JSONDecodeError, dumps, loads


def servchanParse(servchan: str) -> tuple[str | bool | None, str | bool | None]:
    # parse servchan
    server: str | bool | None
    channel: str | bool | None
    if servchan == "-":
        server = False
        channel = False
    elif servchan == "this":
        server = None
        channel = None  # event.target will be set in wrapper
    else:
        if ":#" in servchan:
            server, servchan = servchan.split(":", 1)
            if not server:
                server = False
        if servchan.startswith("#"):
            server = False
            channel = servchan
        else:
            server = servchan
            channel = False
    return server, channel


class EmptyValue:
    pass


def _option_metadata(module_options: object, option: str) -> Option | None:
    if not isinstance(module_options, dict) or option not in module_options:
        return None
    return option_spec(module_options[option])


def config(event: Event, bot: BotLike) -> None:
    """config serverchannel module opt [value]. serverchannel = servername:#channel (channel on server) or
    servername (default for server) or :#channel (channel globally) or #channel (channel on this server) or "-" (default)
    or "this" (current channel (unless PM) current server.) module = "-" for non-module options. value should be JSON
    """
    if event.argument == "save":
        call_in_reactor(Settings.saveOptions)
        bot.say("Done (save is automatically done when setting config values.)")
        return
    elif event.argument:
        servchan, module, opt, value = argumentSplit(event.argument, 4)
    else:
        return bot.say(functionHelp(config))

    # set or get value
    if servchan and module and opt:
        server, channel = servchanParse(servchan)
        metadata: Option | None = None
        if module == "-":
            module = None
            metadata = CORE_OPTIONS.get(opt)
        else:
            if not bot.isModuleAvailable(module):
                return bot.say("module %s not available" % module)
            loaded_module = bot.getModule(module)
            modopts = getattr(loaded_module, "OPTIONS", {})
            metadata = _option_metadata(modopts, opt)

        if metadata and metadata.secret and not event.isPM():
            if value:
                return bot.say(
                    "Use PM to set this option. If this is a password you probably want to change it now."
                )
            else:
                return bot.say("Use PM to inspect secret option metadata.")

        # set value
        if value:
            try:
                value = loads(value)
            except JSONDecodeError as e:
                return bot.say("Error: %s" % e)
            tvalue = type(value)
            if metadata and metadata.type is not tvalue:
                return bot.say(
                    "Incorrect type of %s: %s. Require %s."
                    % (opt, tvalue.__name__, metadata.type.__name__)
                )
            msg = "Set %s(%s)." if metadata and metadata.secret else None
            old = EmptyValue
            try:
                old = bot.getOption(opt, server=server, channel=channel, module=module)
            except AttributeError:
                pass
            except (KeyError, TypeError, ValueError) as e:
                return bot.say("Error: %s" % e)

            # check type of non module option:
            # TODO: some things won't be able to have the same type, like set for modules, allowmodules and such. What do?
            #       Use properties like .admins ??
            if not module and (old is not EmptyValue):
                t = type(old)
                if t is not tvalue:
                    return bot.say(
                        "Incorrect type of %s: %s. Require %s." % (opt, tvalue, t)
                    )
            try:
                bot.setOption(opt, value, server=server, channel=channel, module=module)
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                return bot.say("Error: %s" % e)
            call_in_reactor(Settings.saveOptions)
            if msg:
                bot.say(msg % (opt, servchan))
            else:
                old_text = "unset" if old is EmptyValue else dumps(old)
                bot.say(
                    "Set %s(%s) to %s (was: %s)"
                    % (opt, servchan, dumps(value), old_text)
                )

        # get value
        else:
            if metadata and metadata.writeonly:
                return bot.say(
                    "%s is write-only. %s Type: %s, Default: hidden"
                    % (opt, metadata.description, metadata.type.__name__)
                )
            try:
                value = dumps(
                    bot.getOption(opt, server=server, channel=channel, module=module)
                )
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                return bot.say("Error: %s" % e)
            if metadata:
                default = "hidden" if metadata.secret else dumps(metadata.default)
                bot.say(
                    "Setting for %s(%s) is %s. %s Type: %s, Default: %s"
                    % (
                        opt,
                        servchan,
                        value,
                        metadata.description,
                        metadata.type.__name__,
                        default,
                    )
                )
            else:
                bot.say("Setting for %s(%s) is %s" % (opt, servchan, value))
    else:
        return bot.say(functionHelp(config))


# mappings to methods
mappings = (Mapping(command="config", function=config, admin=True),)
