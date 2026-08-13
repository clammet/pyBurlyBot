from util.event import Event
from util.types import BotLike

# run db query
from util import Mapping
import sqlite3


def admin_dbquery(event: Event, bot: BotLike) -> None:
    query = event.argument
    bot.say("Running: %s" % query)
    try:
        result = bot.dbQuery(query)
    except sqlite3.Error as e:
        return bot.say("Error with query: %s" % e)

    if not result:
        return bot.say("No error, but nothing to display.")
    # good
    for row in result:
        nrow = []
        for key in list(row.keys()):
            nrow.append((key, row[key]))
        bot.say(repr(nrow))


mappings = (Mapping(command="dbquery", function=admin_dbquery, admin=True),)
