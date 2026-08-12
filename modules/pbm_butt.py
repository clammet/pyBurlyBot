"""
butt.py - A (poor) python port of buttbot:
https://code.google.com/p/buttbot/

This is ported almost directly from old BBM
"""

from util import Mapping, pastehelper, argumentSplit, fetchone
import re
import random
try:
	from hyphenate import hyphenate_word
except ImportError:
	raise ImportError('butt.py needs Need TeX Hyphenate: http://nedbatchelder.com/code/modules/hyphenate.py')

OPTIONS = {
	"BUTT_RATE" : (int, "Rate/chance of butt 1/n.", 500),
	"BUTTS" : (bool, "Best of butt.", True),
}

# Words/regexes that are boring/intrinsically unbuttable
STOPWORDS = (r'[^A-Za-z]+', 'butt', 'a', 'an', 'and', 'or', 'but', 'it', 'is',
'its', "it's", 'the', 'of', 'you', 'I', 'your')
RE_STOPWORDS = re.compile(r'^\W*(' + r'|'.join(STOPWORDS) + r')\W*$',
							flags=re.IGNORECASE)

def butt(event, bot):
	""" butt [input].  Use the power of computing to make a sentence dumber.
	Replace random syllables of _input_ with 'butt', courtesy of Knuth:
	https://en.wikipedia.org/wiki/Hyphenation_algorithm
	-- If no input is supplied a choice entry from the bestbutts quote database will be returned.
	e.g. .butt what is love
	"""
	if not event.argument:
		if bot.getOption("BUTTS", module="pbm_butt"):
			# get random bestbutt
			items = bot.dbQuery('''SELECT id, butt FROM butts ORDER BY RANDOM() LIMIT 1;''', func=fetchone)
			if not items:
				bot.say("butt what?")
			else:
				bot.say("%s: %s" % (items[0], items[1]))
		else:	
			bot.say("butt what?")
	else:
		bot.say(buttify(event.argument))

def butts(event, bot):
	""" butts [~del] [input].  Add or delete an entry in the bestbutts quote database.
	If no input is supplied the entire database will be dumped.
	e.g. .butts <@bbm> butt is love
	"""
	if not bot.getOption("BUTTS", module="pbm_butt"): return
	if not event.argument:
		items = bot.dbQuery('''SELECT id, butt FROM butts;''')
		if items:
			pastehelper(bot, basemsg="butts: %s", force=True, altmsg="%s", items=("%s: %s" % (row[0], row[1]) for row in items))
		else:
			bot.say("no butts.")
	else:
		cmd, arg = argumentSplit(event.argument, 2)
		if cmd == "~del":
			bot.dbQuery('''DELETE FROM butts WHERE id = ?;''', (arg, ))
			bot.say("OK.")
		else:
			# add a butts
			bot.dbQuery('''INSERT INTO butts (butt) VALUES(?);''', (event.argument,))
			bot.say("OK.")

def rand_butt(event, bot):
	if event.command: return
	msg = event.msg
	if not msg or not len(msg) > 20:
		return
	if random.randint(1, bot.getOption("BUTT_RATE", module="pbm_butt")) != 1:
		return
	result = buttify(msg)
	# No butt occurred
	if result == msg:
		return
	bot.later(random.randint(2, 8), bot.say, result)

def buttify(msg):
	"""Return buttified msg."""
	buttable_words = [word for word in msg.split() if not RE_STOPWORDS.match(word)]
	butt_passes = len(buttable_words) // 8 + 1
	buttable_words.sort(key=len, reverse=True)

	# Weighted shuffle (by length of word) for pick order, then remove duplicates
	words_to_butt = _weighted_butt_words(buttable_words)
	random.shuffle(words_to_butt)

	while butt_passes > 0 and words_to_butt:
		word = words_to_butt.pop(0)
		matches = list(re.finditer(re.escape(word), msg))
		if not matches:
			# Already butted all instances of this word, purge it so it doesn't get selected again
			words_to_butt = [x for x in words_to_butt if x != word]
			continue
		# random_match is a MatchObject of a random occurrence of word in msg
		random_match = random.choice(matches)
		msg = msg[:random_match.start()] + _butt_word(word) + msg[random_match.end():]
		butt_passes -= 1

	return msg


# RE_SPLIT_PUNCTUATION.match('~testings!!!').groups()
# ('~', 'testing', 's!!!')
RE_SPLIT_PUNCTUATION = re.compile(r'^([^A-Za-z]*)(.*?)([xXsS]?[^A-Za-z]*)$')


def _butt_word(word, butt_pass=0):
	# Split into left punctuation, word, right punctuation on first pass
	lp, actual_word, rp = RE_SPLIT_PUNCTUATION.match(word).groups()

	hyphenated_parts = hyphenate_word(actual_word)
	if butt_pass > 0 and len(hyphenated_parts) == 1:
		return word

	x = 0
	points = [0]
	# Generate 'word' string offsets for splicing
	for part in hyphenated_parts:
		x += len(part)
		points.append(x)

	offset_index = random.randrange(len(points) - 1)

	l = points[offset_index]
	r = points[offset_index + 1] - l
	# Scan left and right to consume all leading b's and trailing t's to avoid e.g.
	# !butt Bartering -> Butttering # triple t
	while (actual_word[l + r: l + r + 1]) == 't':
		r += 1
	while l > 0 and actual_word[l - 1] == 'b':
		l -= 1
	sub = actual_word[l:l+r]
	butt = 'butt'
	if not len(sub):
		sub = actual_word
		l = 0
		r = len(sub)
	if sub.isupper():
		butt = 'BUTT'
	elif sub[0].isupper():
		butt = 'Butt'

	actual_word = actual_word[:l] + butt + actual_word[l+r:]
	if len(hyphenated_parts) > 5 and random.randint(0, (4 - butt_pass)) == 1:
		butt_pass += 1
		actual_word = _butt_word(actual_word, butt_pass=butt_pass)
	return lp + actual_word + rp


def _weighted_butt_words(sortedlist):
	weight = len(sortedlist)
	weighted_butt_words = []
	for word in sortedlist:
		weighted_butt_words.extend([word] * (weight ** 2))
		weight -= 1
	return weighted_butt_words

def init(bot):
	bot.dbCheckCreateTable("butts", 
		'''CREATE TABLE butts(
			id INTEGER PRIMARY KEY,
			butt TEXT
		);''')
	return True

mappings = (Mapping(command="butt", function=butt), Mapping(command="butts", function=butts),
			Mapping(types=["privmsged"], function=rand_butt))
