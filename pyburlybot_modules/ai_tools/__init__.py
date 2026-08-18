"""Tool contract for the bbm AI harness.

This package is not a bot module: bbm imports it directly. Submodules here
(websearch, calculator, weather, ...) each expose

    def get_tools(bot: BotLike) -> tuple[AITool, ...]

and are loaded by bbm's init() according to its "tools" option. Any regular
bot module can also contribute tools by declaring REQUIRES = ("bbm",) and
calling bbm's register_tool(bot, AITool(...)) from its init().

A tool's func runs inside bbm's reply handler (a dispatcher worker thread)
and must return the tool result as a plain string; raise or return an
"Error: ..." string to tell the model what went wrong. Bot modules a tool
depends on go in requires: the tool is only advertised to the model when all
of them are loaded for the current server.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from util.event import Event
from util.types import BotLike


@dataclass(frozen=True, slots=True)
class AITool:
    """One function the model may call.

    parameters is a JSON Schema object describing the arguments; func receives
    the decoded argument dict and a ToolContext.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[["ToolContext", dict[str, Any]], str]
    requires: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool gets to work with: the bot API, the triggering event, and
    mute(seconds), which silences the harness where the event came from."""

    bot: BotLike
    event: Event
    mute: Callable[[float], None]
