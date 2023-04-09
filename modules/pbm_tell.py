#tell module
from time import gmtime, localtime, mktime
from datetime import datetime
from calendar import timegm # silly python... I just want UTC seconds
from collections import deque

from util import Mapping, argumentSplit, functionHelp, distance_of_time_in_words, fetchone,\
	pastehelper, english_list, parseDateTime
from util.settings import ConfigException
# added dependency on user module only for speed. Means can keep reference to user module without having
# to dive in to the reactor twice? per message
# because of this I should do foreign key things but don't want to lock myself in to that just yet (bad@db)
REQUIRES = ("pbm_users",)
USERS_MODULE = None

#nick: <source> msg - time
TELLFORMAT = u"{0}: <{1}> {2} - {3}"
#nick: I'll pass that on when target is around.
RPLFORMAT = u"%s: I'll %s when %s %s around.%s%s%s"
PASSON = u"pass that on"
ASKTHAT = u"ask that"
UNKNOWN = u" Don't know (%s)."
URSELF = u" Use notepad for yourself."
MULTIUSER = u" %s someone once is enough."
#nick: I will remind target about that in timespec.
RPLREMINDFORMAT = u"%s: I will remind %s about that %s.%s%s"
#TARGET, reminder from SOURCE: MSG - set TELLTIME, arrived TOLDTIME.
REMINDFORMAT = u"{0}, reminder from {1}: {2} - set {3}, arrived {4}."
SELFREMINDFORMAT = u"{0}, reminder: {1} - set {2}, arrived {3}."

MAX_REMIND_TIME = 157700000 # 5 year


def _gatherGroupUsers(qfunc, s):
	users = []
	g = USERS_MODULE.ALIAS_MODULE.get_groupname(qfunc, s)
	if g:
		users = ((user, user) for user in USERS_MODULE.ALIAS_MODULE.group_list(qfunc, g))
	return users


def _generate_users(bot, s, nick, skipself=True):
	alias = False
	if bot.isModuleAvailable("pbm_alias"):
		alias = True
	uset = set()
	dupes = False
	users = [] # user,called
	unknown = []
	targets = deque(s.split(","))
	hasself = False
	while targets:
		t = targets.popleft()
		u = USERS_MODULE.get_username(bot, t, nick)
		# check for user, then group (put user in list to make iteration easier)
		if u: 
			u = (u,t),
		elif alias:
			u = _gatherGroupUsers(bot.dbQuery, t)
		
		if u: 
			for iu,it in u:
				if skipself and iu == nick: hasself = True
				else: 
					if iu in uset: dupes = True
					else: 
						users.append((iu, it))
						uset.add(iu)
		else:
			# Note: the following is silly code for allowing of groups/users with commas in them... silly.
			l = [t]
			while not u and targets:
				l.append(targets.popleft())
				u = USERS_MODULE.get_username(bot, ",".join(l), nick)
				if u: 
					u = (u, ",".join(l)),
				elif alias: 
					u = _gatherGroupUsers(bot.dbQuery, ",".join(l))
			# at this point we either have u or ran out of deque, if latter, throw l[1:] back on queue
			if u: 
				for iu,it in u:
					if skipself and iu == nick: hasself = True
					else: 
						if iu in uset: dupes = True
						else:
							users.append((iu,it))
							uset.add(iu)
			else: 
				if l[0]:
					unknown.append(l[0])
				l = l[1:]
				l.reverse()
				targets.extendleft(l)
	return users, unknown, dupes, hasself


def deliver_tell(event, bot):
	# if alias module available use it
	user = None
	if bot.isModuleAvailable("pbm_alias"):
		# pretty convoluted but faster than fetching both modules every time
		# may return none if this event gets captured for a first time user before user module
		# also faster this way than making 2 db calls for USER_MODULE.get_username
		user = USERS_MODULE.ALIAS_MODULE.lookup_alias(bot.dbQuery, event.nick)
	if not user: user = event.nick
	toldtime = int(timegm(gmtime()))
	# This seems like it might be a bit of a waste. But it should stop the rare occurance of "double tell delivery" (I've only seen it happen once.)
	tells = bot.dbBatch(
		(
			# Query1, get tells
			('''SELECT id,source,telltime,origintime,remind,msg FROM tell WHERE user=? AND delivered=0 AND telltime<? ORDER BY telltime;''', (user,toldtime)),
			# Query2, update query
			('''UPDATE tell SET delivered=1,toldtime=? WHERE user=? AND delivered=0 AND telltime<?;''',(toldtime, user, toldtime)),
		)
	)[0] # 0 gets the results from the first query only
	if tells:
		collate = False
		lines = None
		if len(tells) > 3: 
			collate = True
			lines = []
		for tell in tells:
			if tell['remind']:
				source = tell['source']
				if source:
					data = [event.nick, source, tell['msg'], distance_of_time_in_words(tell['origintime'], toldtime), 
						distance_of_time_in_words(tell['telltime'], toldtime, suffix="late")]
					fmt = REMINDFORMAT
				else:
					data = [event.nick, tell['msg'], distance_of_time_in_words(tell['origintime'], toldtime), 
						distance_of_time_in_words(tell['telltime'], toldtime, suffix="late")]
					fmt = SELFREMINDFORMAT
				if collate: lines.append(fmt.format(*data))
				else: bot.say(fmt, strins=data, fcfs=True)
			else:
				data = [event.nick, tell['source'], tell['msg'], distance_of_time_in_words(tell['telltime'], toldtime)]
				if collate: lines.append(TELLFORMAT.format(*data))
				else: bot.say(TELLFORMAT, strins=data, fcfs=True)
		if collate:
			msg = "Tells/reminds for (%s): %%s" % event.nick
			title = "Tells/reminds for (%s)" % event.nick
			pastehelper(bot, msg, items=lines, altmsg="%s", force=True, title=title)


def tell(event, bot):
	""" tell target msg. Will tell a user <target> a message <msg>."""
	target, msg = argumentSplit(event.argument, 2)
	if not target: return bot.say(functionHelp(tell))
	if not msg:
		return bot.say("Need something to tell (%s)" % target)
	users, unknown, dupes, hasself = _generate_users(bot, target, USERS_MODULE.get_username(bot, event.nick))
	
	if not users:
		if hasself: return bot.say("Use notepad.")
		else: return bot.say("Sorry, don't know (%s)." % target)
	
	cmd = event.command.lower()
	
	targets = []
	for user, target in users:
		#cmd user msg
		imsg = "%s %s %s" % (event.command, target, msg)
		# TODO: do we do an alias lookup on event.nick also?
		bot.dbQuery('''INSERT INTO tell(user, telltime, source, msg) VALUES (?,?,?,?);''',
			(user, int(timegm(gmtime())), event.nick, imsg))
		targets.append(target)
	# check if we need to warn about too many tell pastebin
	# https://github.com/Clam-/pyBurlyBot/issues/29 
	#~ n = bot.dbQuery('''SELECT COUNT(id) AS C FROM tell WHERE user = ? AND delivered = ? AND telltime < ?;''', (user, 0, time()), fetchone)['C']
	#~ if n > 3:
		#~ print "GUNNA WARNING"
	if len(users) > 1:
		bot.say(RPLFORMAT % (event.nick, PASSON if cmd == "tell" else ASKTHAT,
			english_list(targets), "are", UNKNOWN % english_list(unknown) if unknown else "",
			URSELF if hasself else "", MULTIUSER % "Telling" if dupes else ""))
	else:
		bot.say(RPLFORMAT % (event.nick, PASSON if cmd == "tell" else ASKTHAT,
			english_list(targets), "is", UNKNOWN % english_list(unknown) if unknown else "",
			URSELF if hasself else "", MULTIUSER % "Telling" if dupes else ""))


def remind(event, bot):
	""" remind target datespec msg. Will remind a user <target> about a message <msg> at <datespec> time.
		datespec can be relative (in) or calendar/day based (on), e.g. 'in 5 minutes'"""
	target, dtime1, dtime2, msg = argumentSplit(event.argument, 4)
	if not target: return bot.say(functionHelp(tell))
	if dtime1.lower() == "tomorrow":
		target, dtime1, msg = argumentSplit(event.argument, 3) # reparse is easiest way I guess... resolves #30 if need to readdress
		dtime2 = ""
	else:
		if not (dtime1 and dtime2): return bot.say("Need time to remind.")
	if not msg:
		return bot.say("Need something to remind (%s)" % target)
	
	origuser = USERS_MODULE.get_username(bot, event.nick)
	users, unknown, dupes, _ = _generate_users(bot, target, origuser, False)

	if not users:
		return bot.say("Sorry, don't know (%s)." % target)
	
	dtime = "%s %s" % (dtime1, dtime2)
	# user location aware destination times
	locmod = None
	goomod = None
	timelocale = False
	try:
		locmod = bot.getModule("pbm_location")
		goomod = bot.getModule("pbm_googleapi")
		timelocale = True
	except ConfigException:
		pass
	
	origintime = timegm(gmtime())
	alocaltime = localtime(origintime)
	localoffset = timegm(alocaltime) - origintime
	if locmod and goomod:
		t = origintime
		loc = locmod.getlocation(bot.dbQuery, origuser)
		if not loc: 
			timelocale = False
			t = alocaltime
		else:
			tz = goomod.google_timezone(loc[1], loc[2], t)
			if not tz: 
				timelocale = False
				t = alocaltime
			else:
				t = gmtime(t + tz[2] + tz[3]) #[2] dst [3] timezone offset
	else:
		t = alocaltime
	ntime = parseDateTime(dtime, t)
	if not ntime: return bot.say("Don't know what time and/or day and/or date (%s) is." % dtime)
	
	# go on, change it. I dare you.
	if timelocale:
		t = timegm(t) - tz[2] - tz[3]
		ntime = ntime - tz[2] - tz[3]
	else:
		t = timegm(t) - localoffset
		ntime = ntime - localoffset

	if ntime < t or ntime > t+MAX_REMIND_TIME:
		return bot.say("Don't sass me with your back to the future reminds.")
	
	targets = []
	for user, target in users:
		if user == origuser: source = None
		else: source = event.nick
		bot.dbQuery('''INSERT INTO tell(user, telltime, origintime, remind, source, msg) VALUES (?,?,?,?,?,?);''',
			(user, int(ntime), int(origintime), 1, source, msg))
		if not source: targets.append("you")
		else: targets.append(target)
	bot.say(RPLREMINDFORMAT % (event.nick, english_list(targets), distance_of_time_in_words(ntime, t),
		UNKNOWN % english_list(unknown) if unknown else "", MULTIUSER % "Reminding" if dupes else ""))


def _user_rename(old, new):
	return (('''UPDATE tell SET user=? WHERE user=?;''', (new, old)),)


def init(bot):
	global USERS_MODULE # oh nooooooooooooooooo
	bot.dbCheckCreateTable("tell", 
		'''CREATE TABLE tell(
			id INTEGER PRIMARY KEY,
			delivered INTEGER DEFAULT 0,
			user TEXT COLLATE NOCASE,
			telltime INTEGER,
			origintime INTEGER,
			toldtime INTEGER,
			remind INTEGER DEFAULT 0,
			source TEXT,
			msg TEXT
		);''')
	# I am bad at indexes.
	bot.dbCheckCreateTable("tell_deliv_idx", '''CREATE INDEX tell_deliv_idx ON tell(user, delivered, telltime);''')
	
	# cache user module.
	# NOTE: you should only call getModule in init() if you have preloaded it first using "REQUIRES"
	USERS_MODULE = bot.getModule("pbm_users")
	# Modules storing "users" in their own tables should register to be notified when a username is changed (by the alias module)
	USERS_MODULE.REGISTER_UPDATE(bot.network, _user_rename)
	return True

mappings = (Mapping(types=["privmsged"], function=deliver_tell),
	Mapping(command=("tell", "ask"), function=tell), Mapping(command="remind", function=remind),)
