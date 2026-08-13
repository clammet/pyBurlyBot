from typing import Any
from urllib.parse import urlencode

from util import Option
from util.http import http
from util.settings import ConfigException
from util.types import BotLike


OPTIONS = {
    "API_KEY": Option(
        str,
        "API key for OpenWeather services.",
        "",
        secret=True,
        writeonly=True,
    ),
}

URL = "https://api.openweathermap.org/data/2.5/%s?%s"


def _query(
    bot: BotLike, endpoint: str, lat: float | str, lon: float | str
) -> dict[str, Any]:
    key = bot.getOption("API_KEY", module="openweathermap_api")
    if not key:
        raise ConfigException("Require API_KEY for OpenWeather API.")
    data = http.get_json(
        URL
        % (
            endpoint,
            urlencode({"appid": key, "lat": lat, "lon": lon, "units": "metric"}),
        )
    )
    if not isinstance(data, dict):
        raise RuntimeError("OpenWeather returned a non-object response.")
    return data


def get_weather(bot: BotLike, lat: float | str, lon: float | str) -> dict[str, Any]:
    """Query OpenWeather for current conditions."""
    return _query(bot, "weather", lat, lon)


def get_forecast(bot: BotLike, lat: float | str, lon: float | str) -> dict[str, Any]:
    """Query OpenWeather for its five-day, three-hour forecast."""
    return _query(bot, "forecast", lat, lon)


def init(bot: BotLike) -> bool:
    return True
