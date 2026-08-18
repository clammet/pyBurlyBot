"""Web search for the bbm harness, backed by the googleapi bot module."""

from typing import Any

from pyburlybot_modules.ai_tools import AITool, ToolContext
from util.types import BotLike


_MAX_RESULTS = 5


def _web_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "Error: no query given."
    try:
        count = int(args.get("count") or 3)
    except (TypeError, ValueError):
        count = 3
    count = min(max(count, 1), _MAX_RESULTS)
    spelling, results = ctx.bot.getModule("googleapi").google(ctx.bot, query, count)
    if not results:
        if spelling:
            return "No results. Did you mean: %s" % spelling
        return "No results."
    lines = ["%s: %s (%s)" % (title, snippet, link) for title, snippet, link in results]
    if spelling:
        lines.insert(0, "Did you mean: %s" % spelling)
    return "\n".join(lines)


def get_tools(bot: BotLike) -> tuple[AITool, ...]:
    return (
        AITool(
            name="web_search",
            description=(
                "Search the web. Use for current events, or facts you are "
                "not sure about. Returns titles, snippets, and links."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms."},
                    "count": {
                        "type": "integer",
                        "description": "Results wanted (1-%d, default 3)."
                        % _MAX_RESULTS,
                    },
                },
                "required": ["query"],
            },
            func=_web_search,
            requires=("googleapi",),
        ),
    )
