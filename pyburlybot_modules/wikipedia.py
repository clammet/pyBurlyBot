# wikipedia module. Built using 
# https://github.com/goldsmith/Wikipedia

# because of this I don't think it can be used for generic mediawikis?

from wikipedia import search, page
from wikipedia.exceptions import DisambiguationError
from util import functionHelp, Mapping

from random import choice

# Random disambiguous page: (SP: ?) Title - body <URL>
RESULT_RPL = "%s{0} - {1} <%s>"

def wiki(event, bot):
	""" wiki \x02searchterm\x02. Will search Wikipedia for \x02searchterm\x02. """
	if not event.argument: return bot.say(functionHelp(wiki))
	result = search(event.argument, results=1, suggestion=True)
	if not result[0]: 
		if result[1]: return bot.say("No results found. Did you mean \x02%s\x02?" % result[1])
		else: return bot.say("No results found.")
	
	errors = []
	attempt = 0
	p = None
	try:
		p = page(result[0]) # use preload=True  when it's fixed: https://github.com/goldsmith/Wikipedia/issues/78
	except DisambiguationError as e:
		errors.append("Random disambig page: ")
		while attempt < 3:
			try: p = page(choice(e.options))
			except DisambiguationError: pass
			attempt += 1
	if not p: return bot.say("Gave up looking for disambiguous entry from disambiguous page.")
	
	if result[1]:
		errors.append("(SP: %s?) " % result[1])
	content = p.content[:800].replace("\n", " ").replace("====", "").replace("===", "").replace("==", "")
	
	bot.say(RESULT_RPL % ("".join(errors), p.url), strins=[p.title, content], fcfs=True)
	

mappings = (Mapping(command=("wiki", "w", "wikipedia"), function=wiki),)
