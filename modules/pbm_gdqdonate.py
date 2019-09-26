# gdq donate. Silly thing.
from util import Mapping
import requests


def gdqdonate(event, bot):
	""" donate """
	r = requests.get("https://taskinoz.com/gdq/api/")
	if r.status_code == 200:
		bot.say(r.text)
	else:
		bot.say("Sad day.")

def init(bot):
	return True

#mappings to methods
mappings = (Mapping(command=("gdqdonate", "donate"), function=gdqdonate),)
