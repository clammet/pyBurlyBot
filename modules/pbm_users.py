#users
from util import Mapping, distance_of_time_in_words, fetchone
# Modules should not import Settings unless you have a very good reason to do so.
from util.settings import Settings

ALIAS_MODULE = None

OPTIONS = {
	"hidden" : (list, "Channels in this list will not be shown in seen requests.", []),
}

# [network] = [statement]
TABLEUPDATES = {}
# [network] = [func]
EXTERNALUPDATES = {}

SEENMSGWSOURCE = 'I last saw %s %s on %s. "%s"'
SEENMSG = 'I last saw %s %s.'

def _user_update(qfunc, event, nick=None):
	#check if exists, then update
	if not nick: nick = event.nick
	qfunc('''INSERT OR REPLACE INTO user (user, host, lastseen, seenwhere, lastmsg) VALUES(?,?,?,?,?);''',
		(nick, event.hostmask, int(event.time), event.target, event.msg))

def user_update(event, bot):
	#check is alias is loaded and available
	# this method gets called on the reactor so it may cause many context switches :(
	if bot.isModuleAvailable("pbm_alias"):
		_user_update(bot.dbQuery, event, ALIAS_MODULE.lookup_alias(bot.dbQuery, event.nick))
	else:
		#alias not loaded
		_user_update(bot.dbQuery, event)
	return

#returns user row, i.e. all user properties in the result
def get_user(bot, nick):
	qfunc = bot.dbQuery
	if bot.isModuleAvailable("pbm_alias"):
		anick = ALIAS_MODULE.lookup_alias(qfunc, nick)
		if anick: return qfunc('''SELECT * FROM user WHERE user=?;''', (anick,), func=fetchone)
	return qfunc('''SELECT * FROM user WHERE user=?;''', (nick,), func=fetchone)

#returns username only, or None if no user exists.
def get_username(bot, nick, source=None, _inalias=False):
	qfunc = bot.dbQuery
	if source and nick.lower() == "me": nick = source
	if _inalias or bot.isModuleAvailable("pbm_alias"):
		alias = ALIAS_MODULE.lookup_alias(qfunc, nick)
		if alias: 
			user = qfunc('''SELECT user FROM user WHERE user=?;''', (alias,), func=fetchone)
			if user: return user['user']
	return _get_username(qfunc, nick)

# get username only. do not look for aliases.
def _get_username(qfunc, nick):
	user = qfunc('''SELECT user FROM user WHERE user=?;''', (nick,), func=fetchone)
	if user: return user['user']

def _user_seen(qfunc, nick):
	return qfunc('''SELECT lastseen, seenwhere, lastmsg FROM user WHERE user = ?;''', (nick, ), fetchone)

def user_seen(event, bot):
	target = event.argument
	if not target:
		return bot.say("Seen who?")
	
	hidden = bot.getOption("hidden", module="pbm_users")
	
	if bot.isModuleAvailable("pbm_alias"):
		# do magic for group
		group = ALIAS_MODULE.group_list(bot.dbQuery, target)
		if group:
			msgs = []
			for member in group:
				seen = _user_seen(bot.dbQuery, member)
				if seen['seenwhere'] in hidden:
					msgs.append(SEENMSG % (target, distance_of_time_in_words(seen['lastseen']) ))
				else:
					msgs.append(SEENMSGWSOURCE % (target, distance_of_time_in_words(seen['lastseen']),
						seen['seenwhere'], seen['lastmsg']))
			if len(group) > 3:
				try:
					return "%s, see %s" % (event.nick, bot.getAddon("paste")("\n".join(msgs), title="Seen %s" % target))
				except AttributeError:
					return bot.say("Too many users and no paste available.")
			else:
				first = True
				for msg in msgs: 
					if first: bot.say("%s, %s" % (event.nick, msg))
					else: bot.say(msg)
					first = False
				return
		
		# not group, look for alias:
		nick = ALIAS_MODULE.lookup_alias(bot.dbQuery, target)
		seen = _user_seen(bot.dbQuery, nick if nick else target)
	else:
		seen = _user_seen(target)
	
	if not seen:
		bot.say("%s, lol dunno." % event.nick)
	else:
		if seen['seenwhere'] in hidden:
			bot.say("%s, %s" % (event.nick, SEENMSG % (target, distance_of_time_in_words(seen['lastseen'])) ))
		else:
			bot.say("%s, %s" % (event.nick, SEENMSGWSOURCE % (target, distance_of_time_in_words(seen['lastseen']),
				seen['seenwhere'], seen['lastmsg'])))
	return

def _rename_user(network, old, new):
	qs = []
	for f in TABLEUPDATES.get(network, []):
		qs.extend(f(old, new))
	qs.append(('''DELETE FROM user WHERE user=?;''', (old,)))
	Settings.databasemanager.batch(network, qs)
	for f in EXTERNALUPDATES.get(network, []):
		f(network, old, new)
	
# passed function MUST return a list of queries to be executed. See tell.py and location.py for examples.
def REGISTER_UPDATE(network, func, external=False):
	if not external:
		TABLEUPDATES.setdefault(network, []).append(func)
	else:
		EXTERNALUPDATES.setdefault(network, []).append(func)

#init should always be here to setup needed DB tables or objects or whatever
def init(bot):
	"""Do startup module things. This just checks if table exists. If not, creates it."""
	bot.dbCheckCreateTable('user',
		'''CREATE TABLE user(
			user TEXT PRIMARY KEY COLLATE NOCASE,
			host TEXT,
			lastseen INTEGER,
			seenwhere TEXT,
			lastmsg TEXT
		);''')

	#should probably index nick column
	#unique does this for us
	#but should probably index lastseen so can ez-tells:
	# if not exists:
	bot.dbCheckCreateTable("user_lastseen_idx", '''CREATE INDEX user_lastseen_idx ON user(lastseen);''')
	return True

#mappings to methods
mappings = (Mapping(types=["privmsged"], function=user_update), Mapping(command="seen", function=user_seen))
