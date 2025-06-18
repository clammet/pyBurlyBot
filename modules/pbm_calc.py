#calc
# caved in and used wolframalpha. Didn't want to combine two services for currency and maths(mathjs was super tempting)

from util import Mapping, commandSplit, functionHelp
from urllib2 import Request, urlopen, HTTPError
from urllib import urlencode
from xml.etree.cElementTree import iterparse

from traceback import format_exc

OPTIONS = {
	"API_KEY" : (unicode, "API key (App ID) for use with WolframAlpha services.", u"not_an_id"),
}

API_KEY = None
URL = "https://api.wolframalpha.com/v2/query?%s"

EXCLUDE_PODS = ("QuadraticResiduesModuloInteger", 'Property', 'ResiduesModuloSmallIntegers',
	'BaseConversions', 'NSidedPolygon', 'NumberLine','Continued fraction', 'ConversionFromOtherUnits', 
	'CorrespondingQuantity', 'ManipulativesIllustration')
EXCLUDE = tuple(zip(("excludepodid",)*len(EXCLUDE_PODS), EXCLUDE_PODS))

POD_PRIORITY = { 'DecimalApproximation' : 0, 'Result' : 1, 'VisualRepresentation' : 100 }

def calc(event, bot):
	""" calc calcquery. Will use WolframAlpha to calc calcquery."""
	if not event.argument: return bot.say(functionHelp(calc))
	s = (("input", event.argument.encode("utf-8")), ("appid", API_KEY), ("reinterpret", "true"),
		("format", "plaintext"), ("podstate", "Rhyme:WordData__More")) + EXCLUDE
		
	# TODO: use "units" param in conjunction with calling user's location.
	
	f = urlopen(URL % (urlencode(s)), timeout=15)
	if f.getcode() == 200:
		# http://effbot.org/zone/element-iterparse.htm
		# get an iterable
		#~ print f.read()
		#~ return
		context = iterparse(f, events=("start", "end"))
		# get the root element
		ievent, root = context.next()
		
		input = None
		results = []
		error = ""
		pod = None
		podnames = []
		priority = 50
		for ievent, elem in context:
			if ievent == "start" and elem.tag == "pod":
				pod = elem.attrib["id"]
				podnames.append((pod, elem.attrib['title'])) # .split(None, 3)[:2]
			elif ievent == "end" and elem.tag == "msg": #assuming msg is only used for error, pls
				results.append("(Error: %s)" % elem.text)
				elem.clear()
			elif ievent == "end" and elem.tag == "plaintext":
				if pod == "Input":
					input = elem.text
				else:
					if elem.text:
						results.append([(pod, priority), "%s" % elem.text.replace("\n ", " ").replace("\n", " ").replace("  ", " ")])
						priority += 1
				elem.clear()
			elif ievent == "end":
				if elem.tag == "pod": 
					pod = None
				elem.clear()
		root.clear()
		# sort results
		#~ print podnames, results
		if not results: 
			if input:
				return bot.say("WolframAlpha doesn't know [%s]." % input)
			else:
				return bot.say("WolframAlpha doesn't know and doesn't understand your input.")
		for entry in results:
			if isinstance(entry, list):
				entry[0] = POD_PRIORITY.get(entry[0][0], entry[0][1])
		results.sort()
		msg = u"[%s] {0}" % (input,)
		#~ print msg, results
		bot.say(msg, strins=[x[1] for x in results], fcfs=True, joinsep=u"\x02,\x02 ")
	else:
		bot.say("Dunno.")

def init(bot):
	global API_KEY # oh nooooooooooooooooo
	API_KEY = bot.getOption("API_KEY", module="pbm_calc")
	return True

#mappings to methods
mappings = (Mapping(command=("calc", "c"), function=calc),)
