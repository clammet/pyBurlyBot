#run db query
from util import Mapping

def admin_dbquery(event, bot):
	query = event.argument
	bot.say("Running: %s" % query)
	try:
		result = bot.dbQuery(query)
	except Exception as e:
		return bot.say("Error with query: %s" % e)

	if not result:
		return bot.say("No error, but nothing to display.")
	print("GOOD")
	#good
	for row in result:
		nrow = []
		for key in list(row.keys()):
			nrow.append((key, row[key]))
		bot.say(repr(nrow))

mappings = (Mapping(command="dbquery", function=admin_dbquery, admin=True),)
