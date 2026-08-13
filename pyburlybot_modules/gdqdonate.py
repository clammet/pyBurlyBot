from util.event import Event
from util.types import BotLike

from util import Mapping
from util.http import HTTPError, HTTPClient


DONATION_API_URL = "https://taskinoz.com/gdq/api/"
REQUEST_TIMEOUT = 15
REQUEST_HEADERS = {
    "User-Agent": "pyBurlyBot/1.0 (+https://github.com/Clam-/pyBurlyBot)"
}
DONATION_HTTP = HTTPClient(timeout=REQUEST_TIMEOUT)


def gdqdonate(event: Event, bot: BotLike) -> None:
    """donate"""
    try:
        response = DONATION_HTTP.get(DONATION_API_URL, headers=REQUEST_HEADERS)
    except (HTTPError, TimeoutError) as exc:
        print(f"GDQ donation feed unavailable: {exc}")
        bot.say("The GDQ donation feed is temporarily unavailable. Try again later.")
        return
    message = response.text.strip()
    if not message:
        print("GDQ donation feed returned an empty response.")
        bot.say("The GDQ donation feed is temporarily unavailable. Try again later.")
        return
    bot.say(message)


def init(bot: BotLike) -> bool:
    return True


mappings = (Mapping(command=("gdqdonate", "donate"), function=gdqdonate),)
