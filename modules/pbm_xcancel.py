from re import compile as recompile
from util import Mapping

def remove_elon(event, bot):
	match = event.regex_match
	posturlportion = match.group(1)
	bot.say("https://xcancel.com/{0}".format(posturlportion))

mappings = (Mapping(types=["privmsged"], regex=recompile(r"https?://(?:x\.com|twitter\.com)/(\S+)", re.IGNORECASE|UNICODE), function=remove_elon),)
