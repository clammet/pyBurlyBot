# Weather Undergroun api module used by weather and location
# http://www.wunderground.com/
# require APIkey module option

from urllib.request import urlopen
from urllib.parse import urlencode
from json import load


from util.settings import ConfigException

OPTIONS = {
	"API_KEY" : (str, "API key for use with Weather Underground services.", "not_a_key"),
}

# key, features, lat, lon
URL = "http://api.wunderground.com/api/%s/%s/q/%s,%s.json"
LOC_URL = "http://autocomplete.wunderground.com/aq?%s"

API_KEY = None
CSE_ID = None

def lookup_location(query):
	""" helper to ask WU for location data. Returns name, lat, lon"""
	f = urlopen(LOC_URL % (urlencode({"query" : query})))
	locdata = load(f)
	if f.getcode() == 200:
		if "RESULTS" in locdata:
			item = locdata["RESULTS"]
			if len(item) == 0:
				return None
			item = locdata["RESULTS"][0]
			return item["name"], item["lat"], item["lon"]
		else:
			return None
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), locdata.replace("\n", " ")))

def get_weather(lat, lon):
	""" helper to ask WU for current weather. Includes forecast also!"""
	if not API_KEY:
		raise ConfigException("Require API_KEY for wuapi. Reload after setting.")
	f = urlopen(URL % (API_KEY, "conditions/forecast", lat, lon))
	weather_data = load(f)
	if f.getcode() == 200:
		if "current_observation" in weather_data:
			return weather_data
		else:
			return None
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), weather_data.replace("\n", " ")))
	
# Not used for weather because doesn't contain "display_location"
def get_forecast(lat, lon):
	""" helper to ask WU for forecasted weather."""
	if not API_KEY:
		raise ConfigException("Require API_KEY for wuapi. Reload after setting.")
	f = urlopen(URL % (API_KEY, "forecast", lat, lon))
	weather_data = load(f)
	if f.getcode() == 200:
		if "forecast" in weather_data:
			forecast = weather_data["forecast"]
			return forecast
		else:
			return None
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), weather_data.replace("\n", " ")))
	

def init(bot):
	global API_KEY # oh nooooooooooooooooo
	API_KEY = bot.getOption("API_KEY", module="wuapi")
	return True
