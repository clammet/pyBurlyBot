from util.event import Event
from util.types import BotLike
from re import compile as recompile, IGNORECASE
from util import Mapping

NITTER = "nitter.cf"

def remove_elon(event: Event, bot: BotLike) -> None:
    match = event.regex_match
    posturlportion = match.group(1)
    bot.say("https://{0}/{1}".format(NITTER, posturlportion))


mappings = (
    Mapping(
        types=["privmsged"],
        regex=recompile(r"https?://(?:x\.com|twitter\.com)/(\S+)", IGNORECASE),
        function=remove_elon,
    ),
)
