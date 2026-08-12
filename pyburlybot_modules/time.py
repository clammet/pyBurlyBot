# time module

from util import Mapping, pastehelper
from time import gmtime, strftime
from calendar import timegm # silly python... I just want UTC seconds

# You could do this without web based service but whatever, offloading is easier. Cloud7.0
REQUIRES = ("location", "googleapi", "users")
LOC_MODULE = None
GAPI_MODULE = None
USERS_MODULE = None

def _processTime(bot, loc, group=False):
	name, lat, lon = loc
	t = timegm(gmtime())
	tz = GAPI_MODULE.google_timezone(lat, lon, t)
	if not group and not tz: return bot.say("Can't find timezone information for (%s, %s, %s)" % (name, lat, lon))
	elif group and not tz: return None
	# gdata["timeZoneId"], gdata["timeZoneName"], gdata["dstOffset"], gdata["rawOffset"]
	t = t + tz[2] + tz[3]
	#TODO: what time format??
	return t, name, tz
	
def ttime(event, bot):
	# attempt group first (because it's easier with current location module weirdness 
	# (getLocationWithError needs rewrite with friendlier API)
	if bot.isModuleAvailable("alias"):
		g = USERS_MODULE.ALIAS_MODULE.get_groupname(bot.dbQuery, event.argument)
		if g:
			users = USERS_MODULE.ALIAS_MODULE.group_list(bot.dbQuery, g)
			# process group request:
			
			if len(users) > 2: collate = True
			else: collate = False
			lines = []
			for u in users:
				success, data = LOC_MODULE.getLocationWithError(bot, u, event.nick, group=True)
				if success:
					tdata = _processTime(bot, data, group=True)
					if tdata:
						t, name, tz = tdata
						if collate: lines.append("(%s) %s - %s (%s-%s)" % (u, strftime("%c", gmtime(t)), name, tz[0], tz[1]))
						else: bot.say("(%s) %s - %s (%s-%s)" % (u, strftime("%c", gmtime(t)), name, tz[0], tz[1]))
					else:
						if collate: lines.append("(%s) Can't find timezone information for (%s, %s, %s)" % (u, data[0], data[1], data[2]))
						else: bot.say("(%s) Can't find timezone information for (%s, %s, %s)" % (u, data[0], data[1], data[2]))
				else:
					if collate: lines.append(data)
					else: bot.say(data)
			if collate:
				msg = "Times for group (%s): %%s" % g
				pastehelper(bot, msg, items=lines, altmsg="%s", force=True, title=msg[:-4])
			return
	# continue if only single user:
	loc = LOC_MODULE.getLocationWithError(bot, event.argument, event.nick)
	if not loc: return
	tdata = _processTime(bot, loc)
	# lookup location offset
	# apply to : timegm(gmtime())
	if tdata:
		t, name, tz = tdata
		bot.say("%s - %s (%s-%s)" % (strftime("%c", gmtime(t)), name, tz[0], tz[1]))
		
def init(bot):
	global LOC_MODULE # oh nooooooooooooooooo
	global GAPI_MODULE # oh nooooooooooooooooo
	global USERS_MODULE # oh nooooooooooooooooo
	
	# cache user module.
	# NOTE: you should only call getModule in init() if you have preloaded it first using "REQUIRES"
	LOC_MODULE = bot.getModule("location")
	GAPI_MODULE = bot.getModule("googleapi")
	USERS_MODULE = bot.getModule("users")
	return True
	
mappings = (Mapping(command=("time","t"), function=ttime),)
