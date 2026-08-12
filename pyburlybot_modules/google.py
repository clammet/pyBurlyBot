# Google search module

from util import Mapping, functionHelp

REQUIRES = ("googleapi",)
GAPI_MODULE = None

# title: snippet (url)
RESULT_SPELL_TEXT = "(SP: %s?) {0}: {1} (%s)"
RESULT_TEXT = "{0}: {1} (%s)"

RESULTS_SPELL_IMG = "(SP: %s?) {0}{1}{2}{3}"
RESULTS_IMG = "{0}{1}{2}{3}"
# title (url)
RESULT_IMG = "%s (%s)"
RESULT_IMG2 = ", %s (%s)"

NUM_IMGS = 4

def google(event, bot):
	""" google searchterm. Will search Google using the provided searchterm."""
	if not event.argument: return bot.say(functionHelp(google))
	spelling, results = GAPI_MODULE.google(event.argument)
	if results:
		item = results[0]
		if spelling:
			rpl = RESULT_SPELL_TEXT % (spelling, item[2])
		else:
			rpl = RESULT_TEXT % item[2]
		bot.say(rpl, fcfs=True, strins=[item[0], item[1]])
	else:
		if spelling:
			bot.say("(SP: %s) No results found." % spelling)
		else:
			bot.say("No results found.")

def google_image(event, bot):
	""" gis searchterm. Will search Google images using the provided searchterm."""
	if not event.argument: return bot.say(functionHelp(google_image))
	spelling, results = GAPI_MODULE.google_image(event.argument, NUM_IMGS)
	#TODO: consider displaying img stats like file size and resolution?
	if results:
		entries = []
		# TODO: the following should probably be handled in the smart unicode cropping thing
		#	or in a smarter generic result splitter thing.
		# TODO: (also) this is basically double iterating over the results. Griff fix later please, thanks.
		for item in results:
			if entries:
				entries.append(RESULT_IMG2 % (item[0], item[1]))
			else:
				entries.append(RESULT_IMG % (item[0], item[1]))
		if len(entries) < NUM_IMGS: entries = entries+[""]*(NUM_IMGS-len(entries))
		
		if spelling:
			bot.say(RESULTS_SPELL_IMG % spelling, fcfs=True, strins=entries)
		else:
			bot.say(RESULTS_IMG, fcfs=True, strins=entries)
	else:
		if spelling:
			bot.say("(SP: %s) No results found." % spelling)
		else:
			bot.say("No results found.")

def init(bot):
	global GAPI_MODULE # oh nooooooooooooooooo
	
	GAPI_MODULE = bot.getModule("googleapi")
	return True

#mappings to methods
mappings = (Mapping(command=("google", "g"), function=google),Mapping(command="gis", function=google_image),)
