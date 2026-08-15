from collections.abc import Callable, Iterable
import sqlite3
from typing import cast
from util.event import Event
from util.types import BotLike, DatabaseQuery
from util.db import Query

# users
from util import Mapping, distance_of_time_in_words, fetchone

# Modules should not import Settings unless you have a very good reason to do so.
from util.settings import Settings

OPTIONS: dict[str, tuple[type, str, list[str]]] = {
    "hidden": (list, "Channels in this list will not be shown in seen requests.", []),
}

# [network] = [statement]
TableUpdate = Callable[[str, str], Iterable[Query]]
ExternalUpdate = Callable[[str, str, str], None]

TABLEUPDATES: dict[str, list[TableUpdate]] = {}
# [network] = [func]
EXTERNALUPDATES: dict[str, list[ExternalUpdate]] = {}

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


def user_update(event: Event, bot: BotLike) -> None:
    # check is alias is loaded and available
    # this method gets called on the reactor so it may cause many context switches :(
    if bot.isModuleAvailable("alias"):
        alias_module = bot.getModule("alias")
        _user_update(
            bot.dbQuery, event, alias_module.lookup_alias(bot.dbQuery, event.nick)
        )
    else:
        # alias not loaded
        _user_update(bot.dbQuery, event)
    return


# returns user row, i.e. all user properties in the result
def get_user(bot: BotLike, nick: str) -> sqlite3.Row | None:
    qfunc = bot.dbQuery
    if bot.isModuleAvailable("alias"):
        anick = bot.getModule("alias").lookup_alias(qfunc, nick)
        if anick:
            return qfunc(
                """SELECT * FROM user WHERE user=?;""", (anick,), func=fetchone
            )
    return qfunc("""SELECT * FROM user WHERE user=?;""", (nick,), func=fetchone)


# returns username only, or None if no user exists.
def get_username(
    bot: BotLike, nick: str, source: str | None = None, _inalias: bool = False
) -> str | None:
    qfunc = bot.dbQuery
    if source and nick.lower() == "me":
        nick = source
    if _inalias or bot.isModuleAvailable("alias"):
        alias = bot.getModule("alias").lookup_alias(qfunc, nick)
        if alias:
            user = qfunc(
                """SELECT user FROM user WHERE user=?;""", (alias,), func=fetchone
            )
            if user:
                return user["user"]
    return _get_username(qfunc, nick)


# get username only. do not look for aliases.
def _get_username(qfunc: DatabaseQuery, nick: str) -> str | None:
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

    if bot.isModuleAvailable("alias"):
        alias_module = bot.getModule("alias")
        # do magic for group
        group = alias_module.group_list(bot.dbQuery, target)
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

        # not group, look for alias:
        nick = alias_module.lookup_alias(bot.dbQuery, target)
        seen = _user_seen(bot.dbQuery, nick if nick else target)
    else:
        seen = _user_seen(bot.dbQuery, target)

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


def _rename_user(network: str, old: str, new: str) -> None:
    qs: list[Query] = []
    for table_update in TABLEUPDATES.get(network, []):
        qs.extend(table_update(old, new))
    qs.append(("""DELETE FROM user WHERE user=?;""", (old,)))
    manager = Settings.databasemanager
    if manager is None:
        raise RuntimeError("Database manager has not been initialized.")
    manager.batch(network, qs)
    for external_update in EXTERNALUPDATES.get(network, []):
        external_update(network, old, new)


# passed function MUST return a list of queries to be executed. See tell.py and location.py for examples.
def REGISTER_UPDATE(
    network: str, func: TableUpdate | ExternalUpdate, external: bool = False
) -> None:
    if not external:
        TABLEUPDATES.setdefault(network, []).append(cast(TableUpdate, func))
    else:
        EXTERNALUPDATES.setdefault(network, []).append(cast(ExternalUpdate, func))


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
