from typing import Any
from urllib.parse import urlencode

from util import Option
from util.http import http
from util.settings import ConfigException
from util.types import BotLike


OPTIONS = {
    "API_KEY": Option(
        str,
        "API key for use with Google services.",
        "",
        secret=True,
        writeonly=True,
    ),
    "CSE_ID": (str, "ID of Custom Search Engine to use with Google search.", ""),
}

SEARCH_URL = "https://www.googleapis.com/customsearch/v1?%s"
LOC_URL = "https://maps.googleapis.com/maps/api/geocode/json?%s"
TIMEZONE_URL = "https://maps.googleapis.com/maps/api/timezone/json?%s"
YOUTUBE_URL = "https://www.googleapis.com/youtube/v3/search?%s"
YOUTUBE_INFO_URL = "https://www.googleapis.com/youtube/v3/videos?%s"


def _api_key(bot: BotLike) -> str:
    key = bot.getOption("API_KEY", module="googleapi")
    if not key:
        raise ConfigException("Require API_KEY for googleapi.")
    return key


def _json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    data = http.get_json(url % urlencode(params))
    if not isinstance(data, dict):
        raise RuntimeError("Google returned a non-object response.")
    return data


def google(
    bot: BotLike, query: str, num_results: int = 1
) -> tuple[str | None, list[tuple[str, str, str]]]:
    """Return Google search results and an optional spelling correction."""
    data = _json(
        SEARCH_URL,
        {
            "q": query,
            "key": _api_key(bot),
            "cx": bot.getOption("CSE_ID", module="googleapi"),
            "num": num_results,
            "fields": "spelling/correctedQuery,items(title,link,snippet)",
        },
    )
    spelling_data = data.get("spelling")
    spelling = (
        spelling_data.get("correctedQuery") if isinstance(spelling_data, dict) else None
    )
    results = [
        (
            str(item.get("title", "")),
            str(item.get("snippet", "")).replace(" \n", " "),
            str(item.get("link", "")),
        )
        for item in data.get("items", [])
        if isinstance(item, dict)
    ]
    return spelling, results


def google_image(
    bot: BotLike, query: str, num_results: int
) -> tuple[str | None, list[tuple[str, str]]]:
    data = _json(
        SEARCH_URL,
        {
            "q": query,
            "key": _api_key(bot),
            "cx": bot.getOption("CSE_ID", module="googleapi"),
            "num": num_results,
            "searchType": "image",
            "fields": "spelling/correctedQuery,items(title,link)",
        },
    )
    spelling_data = data.get("spelling")
    spelling = (
        spelling_data.get("correctedQuery") if isinstance(spelling_data, dict) else None
    )
    results = [
        (str(item.get("title", "")), str(item.get("link", "")))
        for item in data.get("items", [])
        if isinstance(item, dict)
    ]
    return spelling, results


def google_timezone(
    bot: BotLike, lat: float | str, lon: float | str, timestamp: int | float
) -> tuple[str, str, int, int]:
    data = _json(
        TIMEZONE_URL,
        {
            "location": "%s,%s" % (lat, lon),
            "key": _api_key(bot),
            "timestamp": int(timestamp),
        },
    )
    if data.get("status") != "OK":
        raise RuntimeError(
            "Google timezone error: %s" % data.get("errorMessage", data.get("status"))
        )
    return (
        str(data["timeZoneId"]),
        str(data["timeZoneName"]),
        int(data["dstOffset"]),
        int(data["rawOffset"]),
    )


def google_geocode(bot: BotLike, query: str) -> tuple[str, float, float] | None:
    data = _json(
        LOC_URL,
        {"address": query, "key": _api_key(bot)},
    )
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return None
    item = results[0]
    if not isinstance(item, dict):
        return None
    location = item.get("geometry", {}).get("location")
    if not isinstance(location, dict):
        return None
    return (
        str(item["formatted_address"]),
        float(location["lat"]),
        float(location["lng"]),
    )


def google_youtube_search(
    bot: BotLike, query: str, related_to: str | None = None
) -> tuple[int | None, list[dict[str, Any]]]:
    params = {
        "q": query,
        "part": "snippet",
        "key": _api_key(bot),
        "safeSearch": "none",
        "type": "video,channel",
    }
    if related_to:
        params["relatedToVideoId"] = related_to
    data = _json(YOUTUBE_URL, params)
    total = data.get("pageInfo", {}).get("totalResults")
    items = [item for item in data.get("items", []) if isinstance(item, dict)]
    return int(total) if total is not None else None, items


def google_youtube_check(bot: BotLike, video_id: str) -> bool:
    data = _json(
        YOUTUBE_INFO_URL,
        {"id": video_id, "part": "id,status", "key": _api_key(bot)},
    )
    return bool(data.get("items"))


def google_youtube_details(bot: BotLike, video_id: str) -> dict[str, Any] | None:
    data = _json(
        YOUTUBE_INFO_URL,
        {
            "id": video_id,
            "part": "contentDetails,id,snippet,statistics,status",
            "key": _api_key(bot),
        },
    )
    items = data.get("items", [])
    return items[0] if items and isinstance(items[0], dict) else None


def init(bot: BotLike) -> bool:
    return True
