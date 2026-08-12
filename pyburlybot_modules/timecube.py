from util.event import Event
from util.types import BotLike

from util import Mapping
import urllib.request, urllib.parse, urllib.error
import random

OPTIONS = {
	"URL" : (str, "URL of processed timecube data for IRC display", '')
}
IRC_COLORS = (
	(255, 255, 255),
	(0, 0, 0),
	(0, 0, 127),
	(0, 147, 0),
	(255, 0, 0),
	(127, 0, 0),
	(156, 0, 156),
	(252, 127, 0),
	(255, 255, 0),
	(0, 252, 0),
	(0, 147, 147),
	(0, 255, 255),
	(0, 0, 252),
	(255, 0, 255),
	(127, 127, 127)
)


def find_color(hexcolor: str) -> str:
	rgb = [int(y, 16) for y in (hexcolor[0:2], hexcolor[2:4], hexcolor[4:6])]
	sumOfSquares = (9999999, 0)
	for num, irc_color in enumerate(IRC_COLORS):
		diff = sum((x - y) ** 2 for x, y in zip(irc_color, rgb))
		if diff < sumOfSquares[0]:
			sumOfSquares = (diff, num)
	if sumOfSquares[1] in (0,1):
		color = ''
	elif sumOfSquares[1] in (2, 11):
		color = '\x0310'
	else:
		color = '\x03%s' % sumOfSquares[1]
	return color


def timecube(event: Event, bot: BotLike) -> None:
	"""FOUR SIMULTANEOUS EARTH CUBE ROTATIONS."""
	tc_url = bot.getOption("URL", module="timecube")
	if not tc_url:
		return bot.say('Timecube data URL must be set.')
	if event.isPM():
		return bot.say('GENE RAY CREATED TIME CUBE FOR ALL, NOT JUST YOU')

	with urllib.request.urlopen(tc_url) as response:
		tclist = [line.decode("utf-8", "replace") for line in response.readlines()]
	line, color = None, None
	if not event.argument:
		color, line = random.choice(tclist).split('\t', 1)
	else:
		search_string = event.argument
		match_list = []
		for line in tclist:
			if search_string.lower() in line.lower():
				match_list.append(line)
		if match_list:
			color, line = random.choice(match_list).split('\t', 1)
	if line and color:
		color = find_color(color)
		bot.say(color + line.strip())
	else:
		bot.say('Nope.')

mappings = (Mapping(command=("timecube", "tc"), function=timecube),)
