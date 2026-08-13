from util.event import Event
from util.types import BotLike

# random module
from random import choice, randint, random

from util import Mapping, functionHelp, argumentSplit

COIN = ("heads", "tails")


def do_flip(event: Event, bot: BotLike) -> None:
    """coinflip. choice flip will return randomly "heads" or "tails"."""
    if random() > 0.999:
        bot.say("The coin landed on its side.")
    else:
        bot.say("%s" % choice(COIN))


def do_choice(event: Event, bot: BotLike) -> None:
    """choice [value...]. choice will randomly select one of the given values,
    or if only one value a random character from the given value."""
    if not event.argument:
        return bot.say(functionHelp(do_choice))
    values = argumentSplit(event.argument, -1)
    if len(values) == 1:
        value = values[0]
        if value is None:
            return bot.say(functionHelp(do_choice))
        return bot.say("%s" % choice(value))
    elif len(set(values)) == 1:
        # Only duplicate values
        return bot.say("%s, obviously." % values[0])
    return bot.say("%s" % choice(values))


def do_rand(event: Event, bot: BotLike) -> None:
    """rand [arg]. If no arg rand will generate random int between 0-10. If arg is an integer
    value a random int between 0-arg will be generated. arg can also be a list of items, in which case will act like choice."""
    if not event.argument:
        return bot.say("%s" % randint(0, 10))

    try:
        num = int(event.argument)
    except ValueError:
        do_choice(event, bot)
    else:
        if num < 0:
            return bot.say("A number between %d and 0: %d" % (num, randint(num, 0)))
        elif num == 0:
            return bot.say("Zero.")
        else:
            return bot.say("A number between 0 and %d: %d" % (num, randint(0, num)))


mappings = (
    Mapping(command=("rand", "random"), function=do_rand),
    Mapping(command="choice", function=do_choice),
    Mapping(command=("coinflip", "heads", "tails", "flip"), function=do_flip),
)
