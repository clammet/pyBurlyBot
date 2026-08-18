"""Current weather for the bbm harness, via the location and OpenWeather
bot modules. Looks up saved user locations the same way the weather command
does, and reports when a location is unknown so the model can ask for one."""

from typing import Any

from pyburlybot_modules.ai_tools import AITool, ToolContext
from util.types import BotLike


def _c2f(temp_c: float) -> float:
    return (temp_c * 1.8) + 32.0


def _get_weather(ctx: ToolContext, args: dict[str, Any]) -> str:
    bot = ctx.bot
    place = str(args.get("location") or "").strip()
    nick = str(args.get("nick") or "").strip() or (ctx.event.nick or "")
    location_module = bot.getModule("location")
    if place:
        loc = location_module.lookup_location(bot, place)
        if not loc:
            return "Error: no place called %r found." % place
    else:
        user = bot.getModule("users").get_username(bot, nick) if nick else None
        loc = location_module.getlocation(bot.dbQuery, user) if user else None
        if not loc:
            return (
                "No saved location for %s. Ask them where they are, then call "
                "get_weather again with the location parameter." % (nick or "that user")
            )
    name, lat, lon = loc

    weather = bot.getModule("openweathermap_api").get_weather(bot, lat, lon)
    main = weather.get("main", {})
    parts = []
    conditions = weather.get("weather") or []
    if conditions and conditions[0].get("description"):
        parts.append(str(conditions[0]["description"]))
    temp_c = main.get("temp")
    if temp_c is not None:
        parts.append("%.1fC/%.1fF" % (temp_c, _c2f(temp_c)))
    feels_c = main.get("feels_like")
    if feels_c is not None and temp_c is not None and abs(feels_c - temp_c) >= 2:
        parts.append("feels like %.1fC/%.1fF" % (feels_c, _c2f(feels_c)))
    if main.get("humidity") is not None:
        parts.append("humidity %d%%" % main["humidity"])
    wind = weather.get("wind", {}).get("speed")
    if wind is not None:
        parts.append("wind %.1fkm/h" % (wind * 3.6))
    if not parts:
        return "Error: OpenWeather returned no usable conditions for %s." % name
    return "Weather for %s: %s." % (name, ", ".join(parts))


def get_tools(bot: BotLike) -> tuple[AITool, ...]:
    return (
        AITool(
            name="get_weather",
            description=(
                "Current weather. With no arguments it uses the requesting "
                "user's saved location; pass nick for someone else, or "
                "location for an explicit place name. If it reports that no "
                "location is saved, ask the user where they are and retry "
                "with location."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Place name, e.g. 'Lansing, MI'.",
                    },
                    "nick": {
                        "type": "string",
                        "description": "IRC nick whose saved location to use.",
                    },
                },
            },
            func=_get_weather,
            requires=("location", "users", "openweathermap_api"),
        ),
    )
