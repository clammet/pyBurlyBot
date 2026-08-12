# OpenWeather API
# https://openweathermap.org/api
# Requires:
#     API_KEY: https://openweathermap.org/appid
#     Working location module

from urllib.request import urlopen
from json import load
from util.settings import ConfigException

OPTIONS = {
	"API_KEY" : (str, "API key for use with Weather Underground services.", "not_a_key"),
}

# key, features, lat, lon
URL = "https://api.openweathermap.org/data/2.5/%s?appid=%s&lat=%s&lon=%s&units=metric"

API_KEY = None
CSE_ID = None

def get_weather(lat, lon):
	""" Query OpenWeatherMap for current weather conditions.
	https://openweathermap.org/current
	"""
	if not API_KEY:
		raise ConfigException("Require API_KEY for OpenWeather API. Reload after setting.")
	f = urlopen(URL % ('weather', API_KEY, lat, lon))
	weather_data = load(f)
	if f.getcode() == 200:
		return weather_data
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), weather_data.replace("\n", " ")))


# Not used for weather because doesn't contain "display_location"
def get_forecast(lat, lon):
	""" Query OpenWeatherMap for 5 day / 3 hour forecast
	https://openweathermap.org/forecast5
	"""
	if not API_KEY:
		raise ConfigException("Require API_KEY for OpenWeather API. Reload after setting.")
	f = urlopen(URL % ('forecast', API_KEY, lat, lon))
	forecast = load(f)
	if f.getcode() == 200:
		return forecast
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), forecast.replace("\n", " ")))


def get_dailyforecast(lat, lon, days=5):
	""" Query OpenWeatherMap for 16 day / daily forecast data
	https://openweathermap.org/forecast16
	:param days: Number of days for which to get a forecast, current max of 16
	"""
	if not API_KEY:
		raise ConfigException("Require API_KEY for OpenWeather API. Reload after setting.")
	f = urlopen(URL % ('forecast/daily', API_KEY, lat, lon) + '&cnt=%d' % days)
	forecast = load(f)
	if f.getcode() == 200:
		return forecast
	else:
		raise RuntimeError("Error (%s): %s" % (f.getcode(), forecast.replace("\n", " ")))


def init(bot):
	global API_KEY # oh nooooooooooooooooo
	API_KEY = bot.getOption("API_KEY", module="openweathermap_api")
	return True
