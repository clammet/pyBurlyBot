from collections.abc import Sequence
from typing import Any
import sqlite3
from util.event import Event
from util.types import BotLike
from util.db import Query
#alert module
from time import gmtime, localtime, struct_time
from util import Timers, TimerExists
from calendar import timegm
from collections import deque

from util import Mapping, argumentSplit, functionHelp, distance_of_time_in_words,\
	pastehelper, english_list, parseDateTime
from util.settings import ConfigException

TIMER_NAME = 'alert_timer'
REQUIRES = ("users",)
USERS_MODULE: Any = None
TELLDELIVER_OBJ: Any = None
# Seconds
LOOP_INTERVAL = 30.0

MULTIUSER = " %s someone once is enough."
RPL_ALERT_FORMAT = "%s: I will alert %s about that %s.%s%s"
ALERT_FORMAT = "{0}, alert from {1}: {2} - set {3}."
SELF_ALERT_FORMAT = "{0}, alert: {1} - set {2}."
UNKNOWN = " Don't know (%s)."

MAX_REMIND_TIME = 31540000 # 1 year


def _lookup_users(bot: BotLike, users_string: str, caller_nick: str,
	skip_self: bool=True) -> tuple[list[tuple[str, str]], list[str], bool, bool]:
	user_set: set[str] = set()
	has_dupes = False
	users: list[tuple[str, str]] = [] # user,called
	unknown: list[str] = []
	to_lookup_list = deque(users_string.split(","))
	has_self = False
	while to_lookup_list:
		to_lookup = to_lookup_list.popleft()
		looked_up_user = USERS_MODULE.get_username(bot, to_lookup, caller_nick)
		if looked_up_user:
			if skip_self and looked_up_user == caller_nick:
				has_self = True
			elif looked_up_user in user_set:
				has_dupes = True
			else:
				users.append((looked_up_user, to_lookup))
				user_set.add(looked_up_user)
		else:
			unknown.append(to_lookup)
	return users, unknown, has_dupes, has_self


def check_alerts_callback(bot: BotLike) -> None:
	current_time = int(timegm(gmtime()))
	timecheck = current_time + int(LOOP_INTERVAL)
	# This seems like it might be a bit of a waste. But it should stop the rare occurance of "double tell delivery" (I've only seen it happen once.)
	alerts = bot.dbQuery('''SELECT id, target_user, alert_time, created_time, source, source_user, msg
			FROM alert WHERE delivered=0 AND alert_time<? ORDER BY alert_time;''', (timecheck,))


	deliver_now: dict[str, list[sqlite3.Row]] = {}
	deliver_soon: dict[str, list[sqlite3.Row]] = {}
	for a in alerts:
		chan_or_user = a['source'].lower()
		delay = a['alert_time'] - current_time
		if delay <= 0:
			deliver_now.setdefault(chan_or_user, []).append(a)
		else:
			deliver_soon.setdefault(chan_or_user, []).append(a)

	for chan_or_user, alerts in deliver_now.items():
		deliver_alerts(chan_or_user, alerts, bot)

	for chan_or_user, alerts in deliver_soon.items():
		delay = alerts[0]['alert_time'] - current_time
		ids = '_'.join(str(x['id']) for x in alerts)
		timer_name = '%s_%s' % (TIMER_NAME, ids)
		try:
			Timers.addtimer(timer_name, delay, deliver_alerts, reps=1,
							chan_or_user=chan_or_user, alerts=alerts, bot=bot)
		except TimerExists:
			pass


def deliver_alerts(chan_or_user: str | None=None,
	alerts: Sequence[sqlite3.Row] | None=None, bot: BotLike | None=None) -> None:
	if not bot:
		return
	if not alerts:
		return
	current_time = int(timegm(gmtime()))

	filtered_alerts: list[sqlite3.Row] = []
	row_ids: list[str] = []
	for a in alerts:
		id = str(a['id'])
		if bot.dbQuery('''SELECT 1 FROM alert WHERE delivered=0 AND id=?''', (id,)):
			row_ids.append(id)
			filtered_alerts.append(a)
	alerts = filtered_alerts

	if not alerts:
		return

	collate = False
	lines: list[str] | None = None
	if len(alerts) > 3:
		collate = True
		lines = []

	for a in alerts:
		receiving_user = a['target_user']
		source_user = a['source_user']
		if source_user:
			data = [a['target_user'], source_user, a['msg'], distance_of_time_in_words(a['created_time'], current_time)]
			fmt = ALERT_FORMAT
		else:
			data = [a['target_user'], a['msg'], distance_of_time_in_words(a['created_time'], current_time)]
			fmt = SELF_ALERT_FORMAT

		if collate:
			assert lines is not None
			lines.append(fmt.format(*data))
		else:
			bot.sendmsg(chan_or_user, fmt, strins=data, fcfs=True)

	if collate:
		assert lines is not None
		msg = "Alerts for (%s): %%s" % receiving_user
		title = "Alerts for (%s)" % receiving_user
		pastehelper(bot, msg, items=lines, altmsg="%s", force=True, title=title)
	for row_id in row_ids:
		bot.dbQuery('''UPDATE alert SET delivered=1 WHERE id=?;''', (row_id, ))


def alert(event: Event, bot: BotLike) -> None:
	""" alert target datespec msg. Alert a user <target> about a message <msg> at <datespec> time.
		datespec can be relative (in) or calendar/day based (on), e.g. 'in 5 minutes'"""
	target, dtime1, dtime2, msg = argumentSplit(event.argument, 4)
	if not target:
		return bot.say(functionHelp(alert))
	if not dtime1:
		return bot.say("Need time to alert.")
	if dtime1.lower() == "tomorrow":
		target, dtime1, msg = argumentSplit(event.argument, 3) # reparse is easiest way I guess... resolves #30 if need to readdress
		dtime2 = ""
	else:
		if not (dtime1 and dtime2): return bot.say("Need time to alert.")
	if not target:
		return bot.say(functionHelp(alert))
	if not msg:
		return bot.say("Need something to alert (%s)" % target)

	origuser = USERS_MODULE.get_username(bot, event.nick) or event.nick or ""
	users, unknown, dupes, _ = _lookup_users(bot, target, origuser, False)

	if not users:
		return bot.say("Sorry, don't know (%s)." % target)

	dtime = "%s %s" % (dtime1, dtime2)
	# user location aware destination times
	locmod = None
	goomod = None
	timelocale = False
	try:
		locmod = bot.getModule("location")
		goomod = bot.getModule("googleapi")
		timelocale = True
	except ConfigException:
		pass

	origin_time = timegm(gmtime())
	alocal_time = localtime(origin_time)
	local_offset = timegm(alocal_time) - origin_time
	t: struct_time = alocal_time
	tz: Any = None
	if locmod and goomod:
		loc = locmod.getlocation(bot.dbQuery, origuser)
		if not loc:
			timelocale = False
		else:
			tz = goomod.google_timezone(loc[1], loc[2], origin_time)
			if not tz:
				timelocale = False
			else:
				t = gmtime(origin_time + tz[2] + tz[3]) #[2] dst [3] timezone offset
	ntime = parseDateTime(dtime, t)
	if not ntime:
		return bot.say("Don't know what time and/or day and/or date (%s) is." % dtime)

	# go on, change it. I dare you.
	if timelocale:
		assert tz is not None
		current_time = timegm(t) - tz[2] - tz[3]
		ntime = ntime - tz[2] - tz[3]
	else:
		current_time = timegm(t) - local_offset
		ntime = ntime - local_offset

	if ntime < current_time or ntime > (current_time + MAX_REMIND_TIME):
		return bot.say("Don't sass me with your back to the future alerts.")
	if ntime < (current_time + 5):
		return bot.say("2fast")

	targets = []
	for user, target in users:
		if user == origuser:
			source_user = None
		else:
			source_user = event.nick

		if event.isPM():
			chan_or_user = event.nick
		else:
			chan_or_user = event.target

		bot.dbQuery('''INSERT INTO alert(target_user, alert_time, created_time, source, source_user, msg) VALUES (?,?,?,?,?,?);''',
				(user, int(ntime), int(origin_time), chan_or_user, source_user, msg))

		if ntime < (current_time + LOOP_INTERVAL):
			Timers.restarttimer(TIMER_NAME)

		if not source_user:
			targets.append("you")
		else:
			targets.append(target)
	bot.say(RPL_ALERT_FORMAT % (event.nick, english_list(targets), distance_of_time_in_words(ntime, current_time),
		UNKNOWN % english_list(unknown) if unknown else "", MULTIUSER % "Alerting" if dupes else ""))


def _user_rename(old: str, new: str) -> tuple[Query, ...]:
	return ('''UPDATE alert SET target_user=? WHERE target_user=?;''', (new, old)),


def setup_timer(event: Event, bot: BotLike) -> None:
	Timers.addtimer(TIMER_NAME, LOOP_INTERVAL, check_alerts_callback, reps=-1, startnow=False, bot=bot)


def init(bot: BotLike) -> bool:
	global USERS_MODULE # oh nooooooooooooooooo
	bot.dbCheckCreateTable("alert",
		'''CREATE TABLE alert(
			id INTEGER PRIMARY KEY,
			delivered INTEGER DEFAULT 0,
			target_user TEXT COLLATE NOCASE,
			source TEXT,
			source_user TEXT,
			alert_time INTEGER,
			created_time INTEGER,
			msg TEXT
		);''')

	bot.dbCheckCreateTable("alert_deliv_idx", '''CREATE INDEX alert_deliv_idx ON alert(delivered, alert_time);''')

	# cache user module.
	# NOTE: you should only call getModule in init() if you have preloaded it first using "REQUIRES"
	USERS_MODULE = bot.getModule("users")
	# Modules storing "users" in their own tables should register to be notified when a username is changed (by the alias module)
	USERS_MODULE.REGISTER_UPDATE(bot.network, _user_rename)
	return True


mappings = (Mapping(command=("alert", "alarm"), function=alert),
			Mapping(types=("signedon",), function=setup_timer))
