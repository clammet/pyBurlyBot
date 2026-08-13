from util.event import Event
from util.types import BotLike

# word tools
#
from util import Mapping, functionHelp
from util.http import HTTPError
from util.settings import ConfigException

REQUIRES = "wordsapi"


def _service_failed(bot: BotLike, error: Exception) -> None:
    print("Word service request failed: %s" % error)
    bot.say("The word service is unavailable; try again later.")


def spelling(event: Event, bot: BotLike, skipSearch: bool = False) -> None:
    """spelling [query]. Returns spelling suggestions for query."""
    if not event.argument:
        return bot.say(functionHelp(spelling))
    try:
        suggestions = bot.getModule("wordsapi").spell_check(
            bot, event.argument, skipSearch
        )
    except HTTPError as error:
        return _service_failed(bot, error)
    # TODO: Consider using googleapi to do a first pass
    if suggestions is None:
        return bot.say("\x02%s\x02 is spelt correct." % event.argument)
    else:
        if suggestions:
            return bot.say("Spelling suggestions: %s" % ", ".join(suggestions))
        else:
            try:
                suggestion, _ = bot.getModule("googleapi").google(bot, event.argument)
                if suggestion:
                    return bot.say("Google suggests: %s" % suggestion)
            except ConfigException:
                pass
            return bot.say(
                "\x02%s\x02 is spelt wrong but I don't have any suggestions, sorry."
                % event.argument
            )


def dictionary(event: Event, bot: BotLike) -> None:
    """dictionary [query]. Returns definitions for query."""
    if not event.argument:
        return bot.say(functionHelp(dictionary))
    try:
        result = bot.getModule("wordsapi").word_search(bot, event.argument)
    except HTTPError as error:
        return _service_failed(bot, error)
    if not result:
        return spelling(event, bot, skipSearch=True)
    output = [
        "%s: %s" % (group.part_of_speech, "; ".join(group.definitions))
        for group in result.groups
    ]
    return bot.say("Source: %s — %s" % (result.attribution, ". ".join(output)))


def synonym(event: Event, bot: BotLike) -> None:
    """synonym [query]. Returns synonyms for query."""
    if not event.argument:
        return bot.say(functionHelp(synonym))
    try:
        result = bot.getModule("wordsapi").word_synonyms(bot, event.argument)
    except HTTPError as error:
        return _service_failed(bot, error)
    if result is None:
        return spelling(event, bot, skipSearch=True)
    elif not result.words:
        return bot.say("No synonyms found for \x02%s\x02" % event.argument)
    else:
        attribution = "Source: %s — " % result.attribution if result.attribution else ""
        return bot.say(
            "%sSynonyms for (%s): %s"
            % (attribution, event.argument, ", ".join(result.words))
        )


def init(bot: BotLike) -> bool:
    return True


mappings = (
    Mapping(command=("dict", "d", "dictionary"), function=dictionary),
    Mapping(command=("spell", "sp", "spelling"), function=spelling),
    Mapping(command=("syn", "synonym", "thesaurus"), function=synonym),
)
