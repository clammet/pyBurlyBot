#updaterelaunch super update reload module

from subprocess import check_output

# git fetch
# git diff --name-status master origin/master
#M       stuff
#M       things/abc.txt
# git merge origin/master 
from util import Mapping

### Modules should not import this! Unless they have a very good reason to.
from util.settings import Settings

### This is only something that modules that know what they are doing should do:
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread
###

OPTIONS = {
	"git_path" : (str, "Path to git executable.", "git"),
}


#TODO: This won't really play nice when running multiple bot processes at a time.
#	After the first bot process updates, the rest will think they are up-to-date.
#	This could be solved by storing modtimes of modules and core files at launch time and comparing them.
def update(event, bot):
	""" update will check for git update and restart bot if core files need updating. """

	gitpath = bot.getOption("git_path", module="updaterelaunch")
	if not gitpath:
		gitpath = "git"

	check_output([gitpath, "fetch"], text=True)
	changes = check_output([gitpath, "diff", "--name-status", "main", "origin/main"], text=True)
	print("CHANGES:", changes)
	corechange = False
	modchange = False
	for line in changes.splitlines():
		if line.lstrip("M\t").startswith("pyburlybot_modules/") or line.lstrip("A\t").startswith("pyburlybot_modules/"):
			modchange = True
		elif line.endswith(".py"):
			corechange = True
	check_output([gitpath, "merge", "origin/main"], text=True)

	if corechange:
		print("RESTARTING BOT")
		#restart bot
		blockingCallFromThread(reactor, Settings.shutdown, True)

	elif modchange:
		#reload
		if bot.isModuleAvailable("reload"):
			bot.getModule("reload").admin_reload_bot(event, bot)
		else:
			bot.say("Module(s) updated but can't reload. reload module not available.")
	else:
		bot.say("Already up-to date.")


def local_update(event, bot):
	if not bot.getOption('debug'):
		return bot.say('Debug must be enabled for localupdate.')
	print("RESTARTING BOT")
	bot.say('Restarting...')
	blockingCallFromThread(reactor, Settings.shutdown, True)


mappings = (Mapping(command="update", function=update, admin=True),
			Mapping(command="localupdate", function=local_update, admin=True))
