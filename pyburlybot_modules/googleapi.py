from typing import Any
from util.types import BotLike
from urllib.request import urlopen
from urllib.error import URLError
from urllib.parse import urlencode, quote
from json import load

from util.settings import ConfigException	

OPTIONS = {
	"API_KEY" : (str, "API key for use with Google services.", ""),
	"CSE_ID" : (str, "ID of Custom Search Engine to use with Google search.", ""),
}

SEARCH_URL = "https://www.googleapis.com/customsearch/v1?%s"
LOC_URL = "https://maps.googleapis.com/maps/api/geocode/json?%s"
TIMEZONE_URL = "https://maps.googleapis.com/maps/api/timezone/json?%s"
YOUTUBE_URL = "https://www.googleapis.com/youtube/v3/search?%s"
YOUTUBE_INFO_URL = "https://www.googleapis.com/youtube/v3/videos?%s"
API_KEY = None
CSE_ID = None

def google(query: str, num_results: int=1) -> tuple[str | None, list[tuple[str, str, str]]]:
	""" google helper. Will return Google search results using the provided query up to num_results results."""
	if not API_KEY:
		raise ConfigException("Require API_KEY for googleapi. Reload after setting.")
	d = { "q" : query, "key" : API_KEY, "cx" : CSE_ID, "num" : num_results,
		"fields" : "spelling/correctedQuery,items(title,link,snippet)" }
	
	f = urlopen(SEARCH_URL % (urlencode(d)))
	gdata = load(f)
	if f.getcode() == 200:
		results = []
		spelling =  gdata.get("spelling")
		if spelling: spelling = spelling["correctedQuery"]
		if "items" in gdata:
			for item in gdata["items"]:
				snippet = item["snippet"].replace(" \n", " ") if "snippet" in item else ""
				results.append((item["title"], snippet, item["link"]))
		return (spelling, results)
	else:
		raise RuntimeError("Error: %s" % (gdata.replace("\n", " ")))

def google_image(query: str, num_results: int) -> tuple[str | None, list[tuple[str, str]]]:
	""" google image search helper. Will return Google images using the provided query up to num_results results."""
	if not API_KEY:
		raise ConfigException("Require API_KEY for googleapi. Reload after setting.")
	d = { "q" : query, "key" : API_KEY, "cx" : CSE_ID, "num" : num_results, "searchType" : "image",
		"fields" : "spelling/correctedQuery,items(title,link)"}
		#TODO: consider displaying img stats like file size and resolution?
	f = urlopen(SEARCH_URL % (urlencode(d)))
	gdata = load(f)
	if f.getcode() == 200:
		results = []
		spelling =  gdata.get("spelling")
		if spelling: spelling = spelling["correctedQuery"]
		if "items" in gdata:
			for item in gdata["items"]:
				results.append((item['title'], item['link']))
		return (spelling, results)
	else:
		raise RuntimeError("Error: %s" % (gdata.replace("\n", " ")))
		
def google_timezone(lat: float | str, lon: float | str,
	t: int | float) -> tuple[str, str, int, int]:
	""" helper to ask google for timezone information about a location."""
	if not API_KEY:
		raise ConfigException("Require API_KEY for googleapi. Reload after setting.")
	d = { "location" : "%s,%s" % (lat, lon), "key" : API_KEY, "timestamp" : int(t) }
	# I've seen this request fail quite often, so we'll add a retry
	try:
		f = urlopen(TIMEZONE_URL % (urlencode(d)), timeout=1)
	except URLError:
		f = urlopen(TIMEZONE_URL % (urlencode(d)), timeout=2)
	gdata = load(f)
	if f.getcode() == 200:
		return gdata["timeZoneId"], gdata["timeZoneName"], gdata["dstOffset"], gdata["rawOffset"]
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), gdata.replace("\n", " ")))
		
def google_geocode(query: str) -> tuple[str, float, float] | None:
	""" helper to ask google for location data. Returns name, lat, lon"""
	if not API_KEY:
		raise ConfigException("Require API_KEY for googleapi. Reload after setting.")
	d = {"address" : query, "key" : API_KEY }
	f = urlopen(LOC_URL % (urlencode(d)))
	locdata = load(f)
	if f.getcode() == 200:
		if "results" in locdata:
			item = locdata["results"]
			if len(item) == 0:
				return None
			item = item[0]
			ll = item.get("geometry", {}).get("location") # lol tricky
			if not ll: return None
			return item["formatted_address"], ll["lat"], ll["lng"]
		else:
			return None
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), locdata.replace("\n", " ")))
		
def google_youtube_search(query: str,
	relatedTo: str | None=None) -> tuple[int | None, list[dict[str, Any]]]:
	""" helper to ask google for youtube search. returns numresults, results[(title, url)]"""
	# TODO: make module option for safesearch
	if not API_KEY:
		raise ConfigException("Require API_KEY for googleapi. Reload after setting.")
	d = {"q" : query, "part" : "snippet", "key" : API_KEY, "safeSearch" : "none",
		"type" : "video,channel"}
	if relatedTo:
		d["relatedToVideoId"] = relatedTo
	f = urlopen(YOUTUBE_URL % (urlencode(d)))
	ytdata = load(f)
	# TODO: handle "badRequest (400)  invalidVideoId"  for relatedTo
	if f.getcode() == 200:
		numresults = ytdata.get("pageInfo", {}).get("totalResults")
		if "items" in ytdata:
			results = ytdata["items"]
			if len(results) == 0:
				return numresults, []
			return numresults, results
		return numresults, []
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), ytdata.replace("\n", " ")))

def google_youtube_check(id: str) -> bool:
	""" helper to ask google if youtube ID is valid."""
	if not API_KEY:
		raise ConfigException("Require API_KEY for googleapi. Reload after setting.")
	d = {"id" : quote(id), "part" : "id,status", "key" : API_KEY}
	
	f = urlopen(YOUTUBE_INFO_URL % (urlencode(d)))
	ytdata = load(f)
	if not ytdata.get("items"): # if there are no items for the ID search, return False
		return False
	return True
		
def google_youtube_details(vidid: str) -> dict[str, Any] | None:
	""" helper to ask google for youtube video details."""
	if not API_KEY:
		raise ConfigException("Require API_KEY for googleapi. Reload after setting.")
	# TODO: make module option for safesearch
	d = {"id" : quote(vidid), "part" : "contentDetails,id,snippet,statistics,status", "key" : API_KEY}
	
	f = urlopen(YOUTUBE_INFO_URL % (urlencode(d)))
	ytdata = load(f)
	if f.getcode() == 200:
		if "items" in ytdata:
			results = ytdata["items"]
			if len(results) == 0:
				return None
			return results[0]
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), ytdata.replace("\n", " ")))

def init(bot: BotLike) -> bool:
	global API_KEY # oh nooooooooooooooooo
	global CSE_ID # oh nooooooooooooooooo
	API_KEY = bot.getOption("API_KEY", module="googleapi")
	CSE_ID = bot.getOption("CSE_ID", module="googleapi")
	return True
