#location

# module stores users locations and can lookup locations

from util import Mapping, fetchone

REQUIRES = ("users", "googleapi")
USERS_MODULE = None
GAPI_MODULE = None

def getlocation(qfunc, user):
	row = qfunc("""SELECT name, lat, lon FROM location 
				WHERE id == ?""", (user,), fetchone)
	if row: return (row['name'], row['lat'], row['lon'])
	else: return None

# if group, return False,msg if error, True,loc if noerror
def getLocationWithError(bot, arg, nick, group=False):
	target = arg
	user = None
	isself = True
	if target: 
		user = USERS_MODULE.get_username(bot, target, nick)
		if user != USERS_MODULE.get_username(bot, nick): isself = False
	else:
		target = user = USERS_MODULE.get_username(bot, nick)
	if user:
		#get location
		loc = getlocation(bot.dbQuery, user)
		if not loc:
			if isself: 
				if group: return False, "Your location isn't known. Try using location."
				else: return bot.say("Your location isn't known. Try using location.")
			else: 
				if group: return False, "Location not known for (%s). Try getting them to set it." % target
				else: return bot.say("Location not known for (%s). Try getting them to set it." % target)
	else:
		# lookup location
		loc = lookup_location(target)
		if not loc: 
			if group: False, "I don't know where (%s) is." % target
			else: return bot.say("I don't know where (%s) is." % target)
	
	if group: return True, loc
	return loc

def lookup_location(query):
	return GAPI_MODULE.google_geocode(query)
	
def _display_location(bot, user):
	loc = getlocation(bot.dbQuery, user)
	if not loc:
		return bot.say("No location for (%s)." % user)
	else:
		return bot.say("%s: %s" % (user, loc[0]))

def location(event, bot):
	""" location [username/location]. If no argument is provided current location for user will be returned. 
	Otherwise a username's location will be returned or a location will be set for requesting user."""
	target = event.argument
	if not target:
		user = USERS_MODULE.get_username(bot, event.nick)
		loc = getlocation(bot.dbQuery, user)
		return _display_location(bot, user)
	else:
		user = USERS_MODULE.get_username(bot,target)
		if user:
			return _display_location(bot, user)
		else:
			user = USERS_MODULE.get_username(bot, event.nick)
			if not user: return bot.say("Haven't seen you before, try again.")
			#set location
			loc = lookup_location(target)
			if not loc:
				return bot.say("Can't set location. Don't know where (%s) is." % target)
			else:
				bot.dbQuery("""INSERT OR REPLACE INTO location (id, name, lat, lon) 
					VALUES (?,?,?,?);""", (user, loc[0], loc[1], loc[2]))
				return bot.say("Done. Set location to (%s)." % loc[0])
			
# test with user that doesn't have location set
def _user_rename(old, new):
	return \
		("""INSERT OR REPLACE INTO location (id, name, lat, lon) 
			SELECT ?, name, lat,  lon 
			FROM location WHERE id == ?""", (new, old)),\
		("""DELETE FROM location WHERE id == ?""", (old,))

def init(bot):
	global USERS_MODULE # oh nooooooooooooooooo
	global GAPI_MODULE # oh nooooooooooooooooo
	
	#id is id from user table
	bot.dbCheckCreateTable("location", 
		'''CREATE TABLE location(
			id TEXT PRIMARY KEY COLLATE NOCASE,
			name TEXT,
			lat REAL,
			lon REAL
		);''')
	
	# cache user module.
	# NOTE: you should only call getModule in init() if you have preloaded it first using "REQUIRES"
	USERS_MODULE = bot.getModule("users")
	GAPI_MODULE = bot.getModule("googleapi")
	# Modules storing "users" in their own tables should register to be notified when a username is changed (by the alias module)
	USERS_MODULE.REGISTER_UPDATE(bot.network, _user_rename)
	return True
	
mappings = (Mapping(command="location", function=location),)
