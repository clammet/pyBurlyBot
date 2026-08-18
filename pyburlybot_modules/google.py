from util.event import Event
from util.types import BotLike
# Google search module

from util import Mapping, functionHelp

REQUIRES = ("googleapi",)

# title: snippet (url)
RESULT_SPELL_TEXT = "(SP: %s?) {0}: {1} (%s)"
RESULT_TEXT = "{0}: {1} (%s)"

RESULTS_SPELL_IMG = "(SP: %s?) {0}"
RESULTS_IMG = "{0}"
# title (url)
RESULT_IMG = "%s (%s)"

NUM_IMGS = 4


def google(event: Event, bot: BotLike) -> None:
    """google searchterm. Will search Google using the provided searchterm."""
    if not event.argument:
        return bot.say(functionHelp(google))
    spelling, results = bot.getModule("googleapi").google(bot, event.argument)
    if results:
        item = results[0]
        if spelling:
            rpl = RESULT_SPELL_TEXT % (spelling, item[2])
        else:
            rpl = RESULT_TEXT % item[2]
        bot.say(rpl, fcfs=True, strins=[item[0], item[1]])
    else:
        if spelling:
            bot.say("(SP: %s) No results found." % spelling)
        else:
            bot.say("No results found.")


def google_image(event: Event, bot: BotLike) -> None:
    """gis searchterm. Will search Google images using the provided searchterm."""
    if not event.argument:
        return bot.say(functionHelp(google_image))
    spelling, results = bot.getModule("googleapi").google_image(
        bot, event.argument, NUM_IMGS
    )
    # TODO: consider displaying img stats like file size and resolution?
    if results:
        # dropwhole: entries that don't fit are dropped whole, so no URL is
        # ever cut in half
        entries = [RESULT_IMG % (item[0], item[1]) for item in results]
        if spelling:
            bot.say(
                RESULTS_SPELL_IMG % spelling,
                strins=entries,
                joinsep=", ",
                dropwhole=True,
            )
        else:
            bot.say(RESULTS_IMG, strins=entries, joinsep=", ", dropwhole=True)
    else:
        if spelling:
            bot.say("(SP: %s) No results found." % spelling)
        else:
            bot.say("No results found.")


def init(bot: BotLike) -> bool:
    return True


# mappings to methods
mappings = (
    Mapping(command=("google", "g"), function=google),
    Mapping(command="gis", function=google_image),
)
