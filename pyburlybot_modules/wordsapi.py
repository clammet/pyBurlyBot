from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from json import load
from xml.etree.ElementTree import iterparse
from util.settings import ConfigException
from io import BytesIO

OPTIONS = {
	"API_KEY" : (str, "API key for use with Cambridge Dictionaries Online's services.", ""),
}

DICT_ORDER = [
	"dictionaries/british",
	"dictionaries/american-english",
]
	

# https://dictionary.cambridge.org/api/v1/dictionaries/british/entries/toilet/?format=html
SEARCH_URL = "https://dictionary.cambridge.org/api/v1/%s/entries/%s/?format=xml"
DIDYOUMEAN_URL = "https://dictionary.cambridge.org/api/v1/%s/search/didyoumean?%s"
# https://dictionary.cambridge.org/api/v1/dictionaries/american-english/topics/topics/the-buttocks/
SAURUS_URL = "https://dictionary.cambridge.org/api/v1/%s/topics/topics/%s/"

API_KEY = None

def spell_check(query, skipSearch=False):
	if not API_KEY:
		raise ConfigException("Require API_KEY for wordsapi. Reload after setting.")
	if not skipSearch and word_search(query):
		return None
	else:
		r = Request(DIDYOUMEAN_URL % (DICT_ORDER[0], urlencode({"q" : query.lower()})))
		r.add_header("accessKey", API_KEY)
		return load(urlopen(r))['suggestions']

def word_search(query):
	""" word helper. Returns a dictionary entry."""
	if not API_KEY:
		raise ConfigException("Require API_KEY for wordsapi. Reload after setting.")
	for d in DICT_ORDER:
		r = Request(SEARCH_URL % (d, quote(query.lower())))
		r.add_header("accessKey", API_KEY)
		try:
			f = urlopen(r)
		except HTTPError:
			continue
		data = load(f)
		if 'entryContent' in data:				
			data = BytesIO(data['entryContent'].encode("utf-8"))
			context = iterparse(data, events=("end","start"))
			# get the root element
			ievent, root = next(context)
			definitions = [] # [[POS, [defs]],]
			usage = None
			defchild = False
			for ievent, elem in context:
				if ievent == "start" and elem.tag == "def":
					defchild = True
				elif ievent == "end" and elem.tag == "pos":
					definitions.append([elem.text, []])
					elem.clear()
				elif ievent == "end" and elem.tag == "def":
					t = elem.text
					if not t:
						e = elem[-1]
						if e.tag == "x":
							for ie in e.iter("f"):
								t = ie.text
						else:
							t = elem[-1].tail.strip() # get tail (text) of last nested element if there's no main tag text
					if not t: t = "???"
					elif t[-2] == ":": t = t[:-2]
					if usage: 
						t = "(%s) %s" % (usage, t)
					definitions[-1][-1].append(t)
					usage = None
					defchild = False
					for child in elem: # clear children
						for cchild in child: cchild.clear()
						child.clear()
					elem.clear()
				elif ievent == "end" and defchild and elem.tag == "usage":
					usage = elem.text
					elem.clear()
				elif ievent == "end" and not defchild:
					elem.clear()
			root.clear()
			return definitions
	else:
		return None

def word_synonyms(query):
	if not API_KEY:
		raise ConfigException("Require API_KEY for wordsapi. Reload after setting.")
	
	data = None
	for d in DICT_ORDER:
		r = Request(SEARCH_URL % (d, quote(query.lower())))
		r.add_header("accessKey", API_KEY)
		try:
			f = urlopen(r)
		except HTTPError:
			continue
		data = load(f)
	if not data: return None
	
	topics = data.get("topics", [])
	syns = []
	for t in topics:
		tid = t.get("topicId")
		if tid:
			r = Request(SAURUS_URL % (DICT_ORDER[0], tid))
			r.add_header("accessKey", API_KEY)
			data = load(urlopen(r))	
			for entry in data['entries']:
				syns.append(entry['entryId'])
	return syns
	
def init(bot):
	global API_KEY # oh nooooooooooooooooo
	API_KEY = bot.getOption("API_KEY", module="wordsapi")
	return True
