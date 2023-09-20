#alias module
from util import Mapping, fetchone, fetchall, argumentSplit, functionHelp, pastehelper
from sys import modules

REQUIRES = ("pbm_users",)
USERS_MODULE = None


def lookup_alias(qfunc, alias):
	result = qfunc('''SELECT user FROM alias WHERE alias = ?;''', (alias,), fetchone)
	#check rows...
	if not result:
		return None
	try:
		return result["user"]
	except IndexError:
		print "This shouldn't happen, invalid alias?"


def lookup_groupalias(qfunc, alias):
	result = qfunc('''SELECT grp FROM aliasgrpalias WHERE alias = ?;''', (alias,), fetchone)
	if not result: return None
	return result['grp']


def add_alias(qfunc, source, alias):
	qfunc('''INSERT OR REPLACE INTO alias (alias, user) VALUES (?,?);''', (alias, source))


def add_groupalias(qfunc, source, alias):
	qfunc('''INSERT OR REPLACE INTO aliasgrpalias (alias, grp) VALUES (?,?);''', (alias, source))


def alias_list(qfunc, nick):
	result = qfunc('''SELECT alias FROM alias WHERE user = ?;''', (nick,))
	return [row['alias'] for row in result]


def group_list(qfunc, group):
	groupa = lookup_groupalias(qfunc, group)
	if groupa: group = groupa
	result = qfunc('''SELECT user FROM aliasgrp WHERE grp = ?;''', (groupa,))
	return [row['user'] for row in result]

def get_groupname(qfunc, group):
	g = lookup_groupalias(qfunc, group)
	if g: return g
	g = qfunc('''SELECT grp FROM aliasgrp WHERE grp = ?;''', (group,), fetchone)
	if g: return g['grp']


def group_add(qfunc, group, user):
	qfunc('''INSERT OR REPLACE INTO aliasgrp (grp, user) VALUES(?,?);''', (group, user))


def group_check(qfunc, group, nick):
	return qfunc('''SELECT 1 FROM aliasgrp WHERE grp = ? AND user = ?;''', (group, nick), fetchone)


def subscripe(event, bot):
	""" subscripe [groupname]. subscripe will list all groups you are a member of. If groupname is supplied you will become
	a member of groupname"""
	user = USERS_MODULE.get_username(bot, event.nick, _inalias=True)
	if not user: return bot.say("Don't I know you?")
	if event.argument:
		gtarget = get_groupname(bot.dbQuery, event.argument)
		if not gtarget: return bot.say("Group (%s) not found." % event.argument)
		if group_check(bot.dbQuery, gtarget, user):
			return bot.say("You are already a member of (%s)." % gtarget)
		group_add(bot.dbQuery, gtarget, user)
		return bot.say("You are now a member of (%s) group." % gtarget)
			
	else:
		#list groups
		listgroups(bot, user)


def unsubscripe(event, bot):
	""" unsubscripe groupname. unsubscripe will remove you from group groupname"""
	user = USERS_MODULE.get_username(bot, event.nick, _inalias=True)
	if not user: return bot.say("Do I know you?")
	if event.argument:
		gtarget = get_groupname(bot.dbQuery, event.argument)
		if not gtarget: return bot.say("Group (%s) not found." % event.argument)
		if not group_check(bot.dbQuery, gtarget, user):
			return bot.say("You aren't a member of (%s)." % gtarget)
		bot.dbQuery('''DELETE FROM aliasgrp WHERE grp = ? AND user = ?;''', (gtarget, user))
		return bot.say("You have been removed from (%s) group." % gtarget)
	else:
		bot.say(functionHelp(unsubscripe))


def listgroups(bot, user=None):
	# [[group, [aliases]],]
	nglist = []
	if user:
		result = bot.dbQuery('''SELECT grp from aliasgrp WHERE user = ? GROUP BY grp;''', (user,))
	else:
		result = bot.dbQuery('''SELECT grp from aliasgrp GROUP BY grp;''')
	for group in result:
		group = group['grp']
		aliases = [a['alias'] for a in bot.dbQuery('''SELECT alias FROM aliasgrpalias WHERE grp = ? AND alias != ?;''', (group, group))]
		if aliases:
			nglist.append("%s (%s)" % (group, ", ".join(aliases)))
		else:
			nglist.append(group)
	if not nglist:
		if user:
			return bot.say("No groups found for user (%s)." % user)
		else:
			return bot.say("No groups found.")
	if user:
		msg = "Groups for (%s): %%s" % user
	else:
		msg = "Groups: %s"
	return pastehelper(bot, msg, items=nglist, title=msg[:-3])


def listgroupusers(bot, groupname):
	# querying a group
	members = group_list(bot.dbQuery, groupname)
	if members:
		msg = "Users in group (%s): %%s" % (groupname)
		return pastehelper(bot, msg, items=members, title="Members of (%s)" % groupname)
	else:
		return bot.say("Group not found: (%s)" % groupname)


def group(event, bot):
	""" group [groupname [user]]. group will display all groups. If groupname is supplied will list all users in group.
	If groupname and user, add user to group groupname. See additional help for ~del.
	|group ~del groupname [user]. ~del groupname will remove entire group (admin.) ~del groupname user will remove user from group. 
	"""
	arg1, arg2 = argumentSplit(event.argument, 2)
	if arg1 == "~del":
		arg1, arg2, arg3 = argumentSplit(event.argument, 3) # rebind arguments for more params
		# do a delete on group
		if arg2 and arg3:
			# assume arg2 is a groupname to remove arg3 from
			group = get_groupname(bot.dbQuery, arg2)
			if group:
				nick = USERS_MODULE.get_username(bot, arg3, source=event.nick, _inalias=True)
				if not nick:
					return bot.say("User/alias (%s) not found." % arg3)
				if group_check(bot.dbQuery, group, nick):
					bot.dbQuery('''DELETE FROM aliasgrp WHERE grp = ? AND user = ?;''', (group, nick))
					return bot.say("Removed (%s) from (%s)" % (nick, group))
				else:
					return bot.say("User (%s) not found in group (%s)" % (nick, group))
			else:
				return bot.say("Group (%s) not found." % arg2)
		elif arg2:
			#remove entire group
			if not bot.isadmin():
				return bot.say("Sry.")
			group = get_groupname(bot.dbQuery, arg2)
			if group:
				# delete aliases then group
				bot.dbBatch(
					('''DELETE FROM aliasgrpalias WHERE grp = ?;''', (group,), 
					'''DELETE FROM aliasgrp WHERE grp = ?;''', (group,))
				)
				return bot.say("Removed group (%s)" % arg3)
			else:
				return bot.say("Group (%s) not found." % arg3)
		else:
			# ~del help
			return bot.say(functionHelp(group, "~del")) 
	
	if arg2:
		# check if group is already a user first:
		target = lookup_alias(bot.dbQuery, arg1) # check target
		if target:
			return bot.say("Group (%s) is in use by an alias/user already." % arg1)
	
		# binding to a group
		nick = USERS_MODULE.get_username(bot, arg2, source=event.nick, _inalias=True)
		if not nick:
			return bot.say("User/alias (%s) not found or seen." % arg2)
		
		# unalias group
		group = get_groupname(bot.dbQuery, arg1)
		if not group:
			# add new group, so create dummy alias entry:
			add_groupalias(bot.dbQuery, arg1, arg1)
			group = arg1
		if group_check(bot.dbQuery, group, nick):
			return bot.say("User (%s) is already a member of (%s)." % (nick, group))

		group_add(bot.dbQuery, group, nick)
		return bot.say("User (%s) added to group (%s)." % (nick, group))

	elif arg1:
		return listgroupusers(bot, arg1)
	# if nothing else show help
	return listgroups(bot)


# process adding an alias for a group. Returns False is group doesn't exist (for error display in caller)
def aliasgroup(bot, groupname, alias):
	source = lookup_groupalias(bot.dbQuery, groupname)
	if not source:
		source = groupname
		# does group exist
		if not bot.dbQuery('''SELECT grp FROM aliasgrp WHERE grp = ?;''', (source,), fetchone):
			return False
	# check if alias is a user alias first
	user = lookup_alias(bot.dbQuery, alias)
	if user:
		return bot.say("(%s) alias is already in use by user (%s)" % (alias, user))
		
	target = lookup_groupalias(bot.dbQuery, alias)
	if target:
		return bot.say("(%s) is already an alias for (%s)" % (alias, target))
	add_groupalias(bot.dbQuery, source, alias)
	bot.say("(%s) alias added for group (%s)" % (alias, source))


# process adding an alias for a user
def aliasuser(bot, arg1, arg2, source):
	# Query target_user first so we can display error messages in sane order.
	target_user = USERS_MODULE._get_username(bot.dbQuery, arg2)
	if source == target_user: return bot.say("But %s is already %s." % (arg1, arg2))
	# then check to see if alias is already part of a group
	groupname = get_groupname(bot.dbQuery, arg2)
	if groupname:
		return bot.say("(%s) is already in use for group (%s)" % (arg2, groupname))
	
	target = lookup_alias(bot.dbQuery, arg2) # check target
	if target:
		#alias already in use by nnick
		return bot.say("Alias already in use by (%s)" % target)
	# check if target is an existing/seen user.
	# If it is, it means we are probably applying a user as an alias (remove old user in that case)
	# in this case we are going to remove target user and execute all observers to user's rename plans.
	# TODO: Do we want to execute the rename plan regardless?
	#		Example: If a module gets disabled for a time, and in that time, a user gets converted to an alias
	#			that module will not have executed the "rename plan", so deleting an alias and readding it you
	#			would assume fixes it, but it won't because the plan won't get executed. Complicated example.
	target = target_user
	
	if source == target: return bot.say("But %s is already %s." % (arg1, arg2))
	
	# see comments just above
	if target: 
		USERS_MODULE._rename_user(bot.network, target, source)
		# find all groups that alias is a part of, and change membership to use "user" (source)
		bot.dbQuery('''UPDATE aliasgrp SET user=? WHERE user = ?;''', (source, arg2))
	# add origin mapping so that origins can't get aliased
	# this will get called everytime but it's more messy if you check alias independently of alias, no big deal if
	add_alias(bot.dbQuery, source, source) # REPLACEing everytime.
	add_alias(bot.dbQuery, source, arg2)
	
	return bot.say("Added (%s) to (%s)" % (arg2, source))


# delete a user and/or group alias
def del_alias(bot, alias):
	#attempt user alias delete
	origin_user = lookup_alias(bot.dbQuery, alias)
	if origin_user:
		bot.dbQuery('''DELETE FROM alias WHERE alias = ?;''', (alias,))
	# attempt group alias delete
	origin_group = lookup_groupalias(bot.dbQuery, alias)
	if origin_group:
		bot.dbQuery('''DELETE FROM aliasgrpalias WHERE alias = ?;''', (alias,))

	if origin_user and origin_group:
		return bot.say("Alias (%s) for user (%s) and group (%s) removed." % (alias, origin_user, origin_group))
	elif origin_user:
		return bot.say("Alias (%s) for user (%s) removed." % (alias, origin_user))
	elif origin_group:
		return bot.say("Alias (%s) for group (%s) removed." % (alias, origin_group))
	else:
		return bot.say("Alias (%s) not found." % alias)


def alias(event, bot):
	""" alias [target] aliasname. If only aliasname is supplied, aliases for aliasname are retrieved. 
	Otherwise if target is also supplied, aliasname will become an alias of target (can also be a group.)
	See addtional help for ~del.
	|alias ~del aliasname: will remove the alias aliasname.
	"""
	# API is bit different from olde bot, and then group things
	arg1, arg2 = argumentSplit(event.argument, 2)

	if arg1 == "~del":
		if arg2:
			return del_alias(bot, arg2)
		else:
			# show help for del
			return bot.say(functionHelp(alias, "~del"))
	elif arg1 and arg2:
		#binding a new alias
		if arg2.lower() == "me": return bot.say("But you are already yourself.")
		
		source = USERS_MODULE.get_username(bot, arg1, source=event.nick, _inalias=True)
		if not source: 
			# ATTEMPT GROUP
			if aliasgroup(bot, arg1, arg2) is False:
				return bot.say("(%s) is not a group or a user I know. Try '.alias <existing_name> <new_alias>'" % arg1)
			else: 
				return
		# else continue with normal user
		return aliasuser(bot, arg1, arg2, source)
		
	elif arg1:
		#querying an alias
		nick = lookup_alias(bot.dbQuery, arg1)
		if nick:
			aliases = alias_list(bot.dbQuery, nick)
			if aliases:
				msg = "Aliases for (%s): %%s" % arg1
				title = "Aliases for (%s)" % arg1
				return pastehelper(bot, msg, items=aliases, altmsg="%s", title=title)
		# unknown alias or no aliases:
		return bot.say("No aliases for (%s)" % arg1)
		
	# if none of the above, show help	
	bot.say(functionHelp(alias))
	return


#init should always be here to setup needed DB tables or objects or whatever
def init(bot):
	global USERS_MODULE # oh nooooooooooooooooo
	bot.dbCheckCreateTable("alias", 
		'''CREATE TABLE alias(
			alias TEXT PRIMARY KEY COLLATE NOCASE,
			user TEXT COLLATE NOCASE
		);''')
	bot.dbCheckCreateTable("aliasgrp", 
		'''CREATE TABLE aliasgrp(
			grp TEXT COLLATE NOCASE,
			user TEXT COLLATE NOCASE
		);''')
	bot.dbCheckCreateTable("aliasgrpalias", 
		'''CREATE TABLE aliasgrpalias(
			alias TEXT PRIMARY KEY COLLATE NOCASE,
			grp TEXT COLLATE NOCASE
		);''')
	#index alias column
	#Unique does this for us
	#but should index nick column so can do fast "get all alias for nick" queries...
	#	Consider going back to using integer IDs for this? I dunno if indexing integers is faster than TEXT in SQLite
	# if not exists:
	bot.dbCheckCreateTable("alias_user_idx", '''CREATE INDEX alias_user_idx ON alias(user);''')
	bot.dbCheckCreateTable("alias_group_idx", '''CREATE INDEX alias_group_idx ON aliasgrp(grp,user);''')
	bot.dbCheckCreateTable("alias_groupalias_idx", '''CREATE INDEX alias_groupalias_idx ON aliasgrpalias(grp);''')
	
	# cache user module.
	# NOTE: you should only call getModule in init() if you have preloaded it first using "REQUIRES"
	USERS_MODULE = bot.getModule("pbm_users")
	# add backreference to alias module for fast lookup
	# this probably shouldn't normally be done.
	USERS_MODULE.ALIAS_MODULE = modules[__name__]
	return True

#mappings to methods
mappings = (Mapping(command="alias", function=alias), Mapping(command=("subscripe", "sub"), function=subscripe),
	Mapping(command="group", function=group), Mapping(command=("unsubscripe", "unsub"), function=unsubscripe),)
