from util.event import Event
from util.types import BotLike
from re import compile as recompile, IGNORECASE
from util import Mapping

def remove_elon(event: Event, bot: BotLike) -> None:
	match = event.regex_match
	posturlportion = match.group(1)
	bot.say("https://xcancel.com/{0}".format(posturlportion))

mappings = (Mapping(types=["privmsged"], regex=recompile(r"https?://(?:x\.com|twitter\.com)/(\S+)", IGNORECASE), function=remove_elon),)
