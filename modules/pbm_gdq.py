# GDQ

from util import Mapping, argumentSplit
from urllib2 import build_opener, HTTPError
from lxml.html import parse
from json import load
from time import gmtime, strptime
from calendar import timegm # silly python... I just want UTC seconds
from util import Timers, TimerExists
from re import compile as re_compile


OPTIONS = {
	"TWITCH_CLIENTID" : (unicode, "Client ID for use in Twitch API calls.", u""),
}

GDQ_URL = "https://gamesdonequick.com"
TWITCH_API_URL = "https://api.twitch.tv/kraken/channels/gamesdonequick"
RPL = "Current: \x02%s\x02 (%s) Upcoming: {0} \x0f| %s %s"
TWITCH_CLIENTID = None
TIMER_NAME = 'gdq_timer'
LOOP_INTERVAL = 120.0 # Seconds
REPEAT_NOTIFY_TIME = 60*30 # 30mins between same game notifies
R_SCHEDULE_SUBLINK = re_compile(r'href="?(/?schedule/[^"]+)')

FORMAT = u"{0}, GAME ({1}) IS AVAILABLE."

def _searchGame(data, title):
	# try searching for incorrect name in timetable take 3:
	found = False
	upcoming = []
	eta = None
	# Join every 2 entries
	for gdata, etadata in zip(data[0::2], data[1::2]):
		# ignore silly entires:
		if len(gdata) < 4: continue
		if found:
			# fix for mid 2016 in gdata[3]
			upcoming.append("\x02%s\x02 by %s (%s)" % (gdata[1], gdata[2], etadata[0].strip().lstrip("0:")[:-3]))
		elif gdata[1].lower() == title:
			found = True
			eta = etadata[0].strip()
	return upcoming, eta

def modifyNameIter(gamename):
	yield gamename
	if ":" in gamename:
		for x in (gamename.replace(":", ""), gamename.split(":")[0]):
			yield x
		if "two" in gamename: yield gamename.replace(":", "").replace("two", "2")
	if u"\u2013" in gamename:
		yield gamename.split(u"\u2013")[0].strip()
	if "two" in gamename:
		yield gamename.replace("two", "2")
	if "!" in gamename:
		yield gamename.rstrip("!")
	if "the" in gamename:
		yield gamename.replace("the ", "")

def gdq(event, bot):
	""" gdq [gamename,~list,~del gamename]. Show gdq info. If gamename is provided, alert will be given when gamename is seen.
		gamename is searched in the time of the stream game, so "kirby" is possible for all kirby games."""
	gamename = argumentSplit(event.argument, 1)[0]
	if gamename:
		if gamename.startswith("~"):
			#process ~list, ~del
			bot.say("Griff halp pls.")
			return
		item = bot.dbQuery('''SELECT source, source_name, game_text
				FROM gdq_alert WHERE source=? AND source_name=? AND game_text=?; ''', (event.target, event.nick, gamename))
		if item:
			bot.say("I'm already going to tell you about (%s)" % gamename)
			return
		bot.dbQuery('''INSERT INTO gdq_alert(source, source_name, game_text, notified_time) VALUES (?,?,?,?);''',
				(event.target, event.nick, gamename, 0))
		bot.say("I'll let you know when (%s) is on." % gamename)
		return
	upcoming = []
	o = build_opener()
	o.addheaders = [('Client-ID', TWITCH_CLIENTID)]
	f = o.open(TWITCH_API_URL)
	game = "Don't know"
	eta = None
	if f.getcode() == 200:
		data = load(f)
		game = data['game']
		gstart = timegm(strptime(data['updated_at'], "%Y-%m-%dT%H:%M:%SZ")) #"2015-01-06T01:11:32Z" UTC
		ngame = game.lower()
		o = build_opener()
		o.addheaders = [('User-agent', 'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.95 Safari/537.36')]
		# GDQ recently added subpages for schedules in the form of /schedule/##
		# Quick and dirty regex to check for and grab the first mentioned subpage, which is assumed to be the latest GDQ event
		schedule_url = GDQ_URL + '/schedule'
		f = o.open(schedule_url)
		raw_content = f.read()
		match = R_SCHEDULE_SUBLINK.search(raw_content)
		if match:
			schedule_url = GDQ_URL + match.group(1)
		f = o.open(schedule_url)
		# http://stackoverflow.com/a/9920703
		page = parse(f)
		rows = page.xpath("body/div//table/tbody")[0].findall("tr")

		data = []
		for row in rows:
			data.append([c.text_content() for c in row.getchildren()])
		# find current
		upcoming = None
		# try searching for incorrect name in timetable because bads...
		for igametitle in modifyNameIter(ngame):
			upcoming, eta = _searchGame(data, igametitle)
			if upcoming: break
		else:
			if ngame[:4] == "the ":
				upcoming, eta = _searchGame(data, ngame[4:])

		if eta:
			curr = timegm(gmtime())
			neta = timegm(strptime(eta, "%H:%M:%S")) - timegm(strptime("0:00:00", "%H:%M:%S"))
			eta = "%s/%s" % (eta.lstrip("0:")[:-3], (neta - (curr-gstart))/60)
		else: eta = "?"
	if not upcoming: upcoming = ["Don't know"]
	bot.say(RPL % (game, eta, "http://www.twitch.tv/gamesdonequick/popout", "https://gamesdonequick.com/schedule"),
		strins=", ".join(upcoming))

def check_games_callback(bot=None):
	current_time = timegm(gmtime())
	timecheck = current_time - REPEAT_NOTIFY_TIME
	# only notify user 30mins after last notified of same game
	alerts = bot.dbQuery('''SELECT id, source, source_name, game_text, notified_time
			FROM gdq_alert WHERE notified_time<? ORDER BY notified_time;''', (timecheck,))

	o = build_opener()
	o.addheaders = [('Client-ID', TWITCH_CLIENTID)]
	try:
		f = o.open(TWITCH_API_URL)
	except HTTPError as err:
		if err.code == 410:
			print("Request to '%r' received reply 'HTTP 410 - Gone'. \
				This is typically a temporary failure." % TWITCH_API_URL)
		else:
			raise err
	game = "Don't know"
	if f.getcode() == 200:
		data = load(f)
		game = data['game'].lower()

	for alert in alerts:
		if alert['game_text'].lower() in game:
			bot.sendmsg(alert['source'], FORMAT, strins=[alert['source_name'],alert['game_text']])
			bot.dbQuery('''UPDATE gdq_alert SET notified_time=?	WHERE id=?;''', (current_time,alert['id']))

def setup_timer(bot):
	Timers.addtimer(TIMER_NAME, LOOP_INTERVAL, check_games_callback, reps=-1, startnow=False, bot=bot)

def unload():
	#Timers.deltimer(TIMER_NAME)
	pass

def init(bot):
	global TWITCH_CLIENTID # oh nooooooooooooooooo
	TWITCH_CLIENTID = bot.getOption("TWITCH_CLIENTID", module="pbm_gdq")

	bot.dbCheckCreateTable("gdq_alert",
		'''CREATE TABLE gdq_alert(
			id INTEGER PRIMARY KEY,
			source TEXT,
			source_name TEXT,
		 	game_text TEXT,
			notified_time INTEGER
		);''')

	bot.dbCheckCreateTable("gdq_alert_idx", '''CREATE INDEX gdq_alert_idx ON gdq_alert(notified_time);''')
	bot.dbCheckCreateTable("gdq_alert2_idx", '''CREATE INDEX gdq_alert2_idx ON gdq_alert(source, source_name, game_text);''')
	# don't do this atm because twitch API change?
	#setup_timer(bot.container)
	return True

mappings = (Mapping(command=("gdq", "agdq", "sgdq"), function=gdq),)
