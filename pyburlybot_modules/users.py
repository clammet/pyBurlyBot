from collections.abc import Callable, Iterable
import sqlite3
from typing import Any, cast
from util.event import Event
from util.types import BotLike, DatabaseQuery
from util.db import Query

# users
from util import Mapping, distance_of_time_in_words, fetchone

OPTIONS: dict[str, tuple[type, str, list[str]]] = {
    "hidden": (list, "Channels in this list will not be shown in seen requests.", []),
}

# [network] = [statement]
TableUpdate = Callable[[str, str], Iterable[Query]]
ExternalUpdate = Callable[[str, str, str], None]

TABLEUPDATES: dict[str, list[TableUpdate]] = {}
# [network] = [func]
EXTERNALUPDATES: dict[str, list[ExternalUpdate]] = {}

# Identity hooks. Modules that extend canonical-user resolution (e.g. alias)
# register per-network functions here from their init(); users itself stays
# ignorant of who provides them. Empty registry means nick == canonical user.
NickResolver = Callable[[DatabaseQuery, str], str | None]
GroupExpander = Callable[[DatabaseQuery, str], list[str]]

# [network] = [func]
NICKRESOLVERS: dict[str, list[NickResolver]] = {}
GROUPEXPANDERS: dict[str, list[GroupExpander]] = {}

SEENMSGWSOURCE = 'I last saw %s %s on %s. "%s"'
SEENMSG = "I last saw %s %s."


def _user_update(qfunc: DatabaseQuery, event: Event, nick: str | None = None) -> None:
    # check if exists, then update
    if not nick:
        nick = event.nick
    if nick is None:
        return
    last_message: str | None
    if event.kwargs.get("is_admin_command"):
        last_message = "Command"
    elif event.isPM():
        last_message = "Private message"
    else:
        last_message = event.msg
    qfunc(
        """INSERT OR REPLACE INTO user (user, host, lastseen, seenwhere, lastmsg) VALUES(?,?,?,?,?);""",
        (nick, event.hostmask, int(event.time), event.target, last_message),
    )


# canonical username for nick per registered resolvers, or None if no
# resolver claims it. Does not check the user table.
def resolve_nick(bot: BotLike, nick: str | None) -> str | None:
    if nick is None:
        return None
    for resolver in NICKRESOLVERS.get(bot.network, []):
        canonical = resolver(bot.dbQuery, nick)
        if canonical:
            return canonical
    return None


# members of the group `name` per registered expanders, or [] if none match.
def expand_group(bot: BotLike, name: str | None) -> list[str]:
    if not name:
        return []
    for expander in GROUPEXPANDERS.get(bot.network, []):
        members = expander(bot.dbQuery, name)
        if members:
            return members
    return []


def user_update(event: Event, bot: BotLike) -> None:
    _user_update(bot.dbQuery, event, resolve_nick(bot, event.nick))
    return


# returns user row, i.e. all user properties in the result
def get_user(bot: BotLike, nick: str) -> sqlite3.Row | None:
    canonical = resolve_nick(bot, nick)
    return bot.dbQuery(
        """SELECT * FROM user WHERE user=?;""", (canonical or nick,), func=fetchone
    )


# returns username only, or None if no user exists.
def get_username(bot: BotLike, nick: str, source: str | None = None) -> str | None:
    qfunc = bot.dbQuery
    if source and nick.lower() == "me":
        nick = source
    canonical = resolve_nick(bot, nick)
    if canonical:
        user = qfunc(
            """SELECT user FROM user WHERE user=?;""", (canonical,), func=fetchone
        )
        if user:
            return user["user"]
    return get_username_raw(qfunc, nick)


# get username only. do not consult resolvers (aliases).
def get_username_raw(qfunc: DatabaseQuery, nick: str) -> str | None:
    user = qfunc("""SELECT user FROM user WHERE user=?;""", (nick,), func=fetchone)
    if user:
        return user["user"]
    return None


def _user_seen(qfunc: DatabaseQuery, nick: str) -> sqlite3.Row | None:
    return qfunc(
        """SELECT lastseen, seenwhere, lastmsg FROM user WHERE user = ?;""",
        (nick,),
        fetchone,
    )


def user_seen(event: Event, bot: BotLike) -> str | None:
    target = event.argument
    if not target:
        return bot.say("Seen who?")

    hidden = bot.getOption("hidden", module="users")

    # do magic for group
    group = expand_group(bot, target)
    if group:
        msgs = []
        for member in group:
            seen = _user_seen(bot.dbQuery, member)
            if seen is None:
                continue
            if seen["seenwhere"] in hidden:
                msgs.append(
                    SEENMSG % (target, distance_of_time_in_words(seen["lastseen"]))
                )
            else:
                msgs.append(
                    SEENMSGWSOURCE
                    % (
                        target,
                        distance_of_time_in_words(seen["lastseen"]),
                        seen["seenwhere"],
                        seen["lastmsg"],
                    )
                )
        if len(group) > 3:
            try:
                return bot.say(
                    "%s, see %s"
                    % (
                        event.nick,
                        bot.getAddon("paste")(
                            "\n".join(msgs), title="Seen %s" % target
                        ),
                    )
                )
            except AttributeError:
                return bot.say("Too many users and no paste available.")
        else:
            first = True
            for msg in msgs:
                if first:
                    bot.say("%s, %s" % (event.nick, msg))
                else:
                    bot.say(msg)
                first = False
            return None

    # not group, resolve to canonical user if possible:
    nick = resolve_nick(bot, target)
    seen = _user_seen(bot.dbQuery, nick if nick else target)

    if not seen:
        bot.say("%s, lol dunno." % event.nick)
    else:
        if seen["seenwhere"] in hidden:
            bot.say(
                "%s, %s"
                % (
                    event.nick,
                    SEENMSG % (target, distance_of_time_in_words(seen["lastseen"])),
                )
            )
        else:
            bot.say(
                "%s, %s"
                % (
                    event.nick,
                    SEENMSGWSOURCE
                    % (
                        target,
                        distance_of_time_in_words(seen["lastseen"]),
                        seen["seenwhere"],
                        seen["lastmsg"],
                    ),
                )
            )
    return None


# migrate a user's rows (here and in every registered observer) from old to new
def rename_user(bot: BotLike, old: str, new: str) -> None:
    network = bot.network
    qs: list[Query] = []
    for table_update in TABLEUPDATES.get(network, []):
        qs.extend(table_update(old, new))
    qs.append(("""DELETE FROM user WHERE user=?;""", (old,)))
    bot.dbBatch(qs)
    for external_update in EXTERNALUPDATES.get(network, []):
        external_update(network, old, new)


# registrations dedupe because a per-server module reload re-runs init() on
# cached module objects, so the same function would be appended again
def _register(registry: dict[str, list[Any]], network: str, func: Any) -> None:
    funcs = registry.setdefault(network, [])
    if func not in funcs:
        funcs.append(func)


# passed function MUST return a list of queries to be executed. See tell.py and location.py for examples.
def REGISTER_UPDATE(
    network: str, func: TableUpdate | ExternalUpdate, external: bool = False
) -> None:
    if not external:
        _register(TABLEUPDATES, network, cast(TableUpdate, func))
    else:
        _register(EXTERNALUPDATES, network, cast(ExternalUpdate, func))


# register a nick -> canonical-user resolver (see resolve_nick)
def REGISTER_RESOLVER(network: str, func: NickResolver) -> None:
    _register(NICKRESOLVERS, network, func)


# register a groupname -> members expander (see expand_group)
def REGISTER_GROUP_EXPANDER(network: str, func: GroupExpander) -> None:
    _register(GROUPEXPANDERS, network, func)


# init should always be here to setup needed DB tables or objects or whatever
def init(bot: BotLike) -> bool:
    """Do startup module things. This just checks if table exists. If not, creates it."""
    bot.dbCheckCreateTable(
        "user",
        """CREATE TABLE user(
            user TEXT PRIMARY KEY COLLATE NOCASE,
            host TEXT,
            lastseen INTEGER,
            seenwhere TEXT,
            lastmsg TEXT
        );""",
    )

    # should probably index nick column
    # unique does this for us
    # but should probably index lastseen so can ez-tells:
    # if not exists:
    bot.dbCheckCreateTable(
        "user_lastseen_idx", """CREATE INDEX user_lastseen_idx ON user(lastseen);"""
    )
    return True


# mappings to methods
mappings = (
    Mapping(types=["privmsged"], function=user_update),
    Mapping(command="seen", function=user_seen),
)
