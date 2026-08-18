"""bbm: an AI chat harness.

Responds to channel lines that mention the bot's nickname (and to private
messages) by asking an AI model for a reply, giving it recent channel
history as context and a set of callable tools. Replies are constrained to a
configurable number of IRC lines; overflow is published through the "paste"
addon when one is loaded.

Two backends (the "backend" option): "openai" uses the openai_api Chat
Completions helper with the function-calling tool harness below; "codex"
uses the codex_api app-server helper (a ChatGPT subscription through the
official codex protocol), where codex brings its own tools instead of the
harness ones and a leading "[sleep N]" reply directive stands in for the
sleep tool.

The tool harness is extensible in two ways: ai_tools submodules named in the
"tools" option are loaded at init (see pyburlybot_modules/ai_tools/), and any
bot module can declare REQUIRES = ("bbm",) and call register_tool() from its
init(). A built-in sleep tool mutes the harness in a channel when someone
tells the bot to shut up.

After the bot replies to someone, that person can continue the conversation
without repeating the nick for a limited (configurable) number of follow-ups.
"""

from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from json import JSONDecodeError, loads
from logging import getLogger
from re import IGNORECASE, Pattern, compile as recompile, escape
from threading import Lock
from time import gmtime, strftime, time
from typing import Any

from pyburlybot_modules import codex_api, openai_api
from pyburlybot_modules.ai_tools import AITool, ToolContext
from util import Mapping, Option, irc_casefold
from util.event import Event
from util.types import BotLike

log = getLogger(__name__)

REQUIRES = ("openai_api", "codex_api")

DEFAULT_PERSONALITY = (
    "You are a laid-back IRC regular: helpful, concise, and a little wry. "
    "Match the channel's tone."
)

OPTIONS = {
    "backend": (
        str,
        "AI backend: openai (Chat Completions, harness tools) or codex "
        "(codex app-server / ChatGPT subscription, codex's own tools).",
        "openai",
    ),
    "model": (str, "Model requested from the openai_api service.", "gpt-5-mini"),
    "system_prompt": (
        str,
        "Personality/tone instructions prepended to every request.",
        DEFAULT_PERSONALITY,
    ),
    "temperature": Option(
        float, "Sampling temperature; null sends the model default.", None
    ),
    "max_completion_tokens": (
        int,
        "Upper bound on tokens generated per reply (0 sends no bound).",
        700,
    ),
    "extra_params": (
        dict,
        'Extra chat-request payload entries, e.g. {"reasoning_effort": "low"}.',
        {},
    ),
    "context_lines": (int, "Recent channel lines included as context.", 30),
    "max_followups": (
        int,
        "Messages from the same person answered without re-mentioning the nick.",
        2,
    ),
    "followup_window": (int, "Seconds a follow-up conversation stays open.", 120),
    "max_lines": (
        int,
        "Most IRC lines a reply may use; overflow goes to the paste addon.",
        2,
    ),
    "sleep_time": (
        int,
        "Default seconds to stay quiet when told to shut up.",
        600,
    ),
    "tool_rounds": (int, "Most tool-call rounds resolved per reply.", 4),
    "tools": (
        list,
        "ai_tools submodules loaded at init.",
        ["calculator", "weather", "websearch"],
    ),
    "nicknames": (
        list,
        "Extra names that count as a mention besides the current nick.",
        [],
    ),
    "ignore": (list, "Nicks never answered (e.g. other bots).", []),
}

HARNESS_PROMPT = (
    "You are the IRC bot %(nick)s in %(where)s on the %(network)s network. "
    "Reply as plain IRC text, no markdown: at most %(max_lines)d short lines, "
    "each well under 400 characters. Messages appear as '<nick> text'; use the "
    "recent-messages block to answer questions about the conversation, such as "
    "what someone was talking about. Speak directly to the current speaker "
    "without prefixing your own nick. %(tools_note)s Stay "
    "within the line limit; only if the full detail is essential or explicitly "
    "requested may you exceed it, in which case the harness publishes the "
    "overflow as a paste link. Current UTC time: %(time)s."
)
OPENAI_TOOLS_NOTE = (
    "Use the tools for arithmetic, current facts, weather, and going quiet; "
    "if a tool reports a missing detail (like an unknown location), ask the "
    "speaker a brief follow-up question."
)
CODEX_TOOLS_NOTE = (
    "You may use your own web search for current facts, but never run "
    "commands. If the speaker tells you to shut up, be quiet, or similar, "
    'begin your reply with the exact text "[sleep N]" (N = minutes to stay '
    'quiet; bare "[sleep]" uses the default) followed by a short sign-off.'
)

HISTORY_MAX = 200
# user/assistant turns retained per follow-up conversation
CONVERSATION_KEEP = 12
# sweep cadence for _gc, and how long an untouched history key (parted
# channel, one-off PM peer) is kept before its ring buffer is dropped
GC_INTERVAL = 300.0
HISTORY_IDLE_SECS = 24 * 60 * 60
_NICK_CHARS = r"A-Za-z0-9\[\]\\`_^{|}~-"


@dataclass
class _Conversation:
    messages: list[openai_api.ChatMessage] = field(default_factory=list)
    remaining: int = 0
    expires: float = 0.0


# module state; rebuilt fresh on reload because the module is re-imported.
# keys carry the network so multi-server bots don't share channels or tools.
# every dict except _tools is swept by _gc so none grows without bound.
_history: dict[tuple[str, str], deque[str]] = {}
_history_seen: dict[tuple[str, str], float] = {}
_conversations: dict[tuple[str, str, str], _Conversation] = {}
_muted: dict[tuple[str, str], float] = {}
_tools: dict[str, dict[str, AITool]] = {}
_lock = Lock()
# next _gc sweep time; single-slot mutable so no global statement is needed
_gc_due = {"at": 0.0}


def register_tool(bot: BotLike, tool: AITool) -> None:
    """Add (or replace) a tool for the harness on this bot's network.

    Modules providing tools should declare REQUIRES = ("bbm",) and call this
    from init(); registrations are per server and reset on module reload."""
    _tools.setdefault(bot.network, {})[tool.name] = tool


def unregister_tool(bot: BotLike, name: str) -> None:
    _tools.get(bot.network, {}).pop(name, None)


def _sleep(ctx: ToolContext, args: dict[str, Any]) -> str:
    try:
        minutes = float(args["minutes"])
    except (KeyError, TypeError, ValueError):
        minutes = float(ctx.bot.getOption("sleep_time", module="bbm")) / 60.0
    seconds = min(max(minutes * 60.0, 30.0), 60.0 * 60.0 * 24.0)
    ctx.mute(seconds)
    return "You are now muted here for %d minutes. Give a short sign-off." % round(
        seconds / 60.0
    )


SLEEP_TOOL = AITool(
    name="sleep",
    description=(
        "Go quiet in this channel for a while. Call this when told to shut "
        "up, be quiet, go away, or similar."
    ),
    parameters={
        "type": "object",
        "properties": {
            "minutes": {
                "type": "number",
                "description": "How long to stay quiet; omit for the default.",
            }
        },
    },
    func=_sleep,
)


def _history_for(network: str, target: str) -> deque[str]:
    key = (network, irc_casefold(target))
    _history_seen[key] = time()
    return _history.setdefault(key, deque(maxlen=HISTORY_MAX))


def _gc(now: float) -> None:
    """Drop state nothing will read again: expired conversations and mutes,
    and history for targets idle past HISTORY_IDLE_SECS. Without this, a
    long-lived process accumulates an entry per nick or channel ever seen."""
    with _lock:
        if now < _gc_due["at"]:
            return
        _gc_due["at"] = now + GC_INTERVAL
        for convo_key, conversation in list(_conversations.items()):
            if conversation.expires < now:
                _conversations.pop(convo_key, None)
        for mute_key, muted_until in list(_muted.items()):
            if muted_until < now:
                _muted.pop(mute_key, None)
        for history_key, last_seen in list(_history_seen.items()):
            if now - last_seen > HISTORY_IDLE_SECS:
                _history_seen.pop(history_key, None)
                _history.pop(history_key, None)


def _format_line(nick: str, text: str) -> str:
    return "<%s> %s" % (nick, text)


@lru_cache(maxsize=8)
def _mention_regex(names: tuple[str, ...]) -> Pattern[str] | None:
    alternatives = "|".join(escape(name) for name in names if name)
    if not alternatives:
        return None
    return recompile(
        "(?<![%s])(?:%s)(?![%s])" % (_NICK_CHARS, alternatives, _NICK_CHARS),
        IGNORECASE,
    )


def _mentioned(event: Event, bot: BotLike) -> bool:
    try:
        current_nick = bot.nickname
    except ValueError:
        current_nick = None
    extra = bot.getOption("nicknames", module="bbm")
    names = tuple(
        dict.fromkeys(
            name for name in (current_nick, *extra) if isinstance(name, str) and name
        )
    )
    pattern = _mention_regex(names)
    return bool(pattern and event.msg and pattern.search(event.msg))


def _mute(key: tuple[str, str], seconds: float) -> None:
    with _lock:
        _muted[key] = time() + max(float(seconds), 0.0)


def _available_tools(bot: BotLike) -> dict[str, AITool]:
    registered = _tools.get(bot.network, {})
    return {
        name: tool
        for name, tool in registered.items()
        if all(bot.isModuleAvailable(module) for module in tool.requires)
    }


def _run_tool(
    available: dict[str, AITool], ctx: ToolContext, call: openai_api.ToolCall
) -> str:
    tool = available.get(call.name)
    if tool is None:
        return "Error: unknown tool %r." % call.name
    try:
        args = loads(call.arguments or "{}")
    except JSONDecodeError:
        return "Error: tool arguments were not valid JSON."
    if not isinstance(args, dict):
        return "Error: tool arguments must be a JSON object."
    try:
        return str(tool.func(ctx, args))
    except Exception as exc:
        log.exception("bbm tool %r failed", call.name)
        return "Error: %s: %s" % (type(exc).__name__, exc)


def _chat(
    bot: BotLike,
    messages: list[openai_api.ChatMessage],
    specs: list[openai_api.ToolSpec],
) -> openai_api.ChatResult:
    temperature = bot.getOption("temperature", module="bbm")
    max_completion_tokens = int(bot.getOption("max_completion_tokens", module="bbm"))
    return openai_api.chat(
        bot,
        messages,
        model=bot.getOption("model", module="bbm"),
        tools=specs,
        temperature=float(temperature) if temperature is not None else None,
        max_completion_tokens=max_completion_tokens or None,
        extra_params=bot.getOption("extra_params", module="bbm") or None,
    )


def _bot_nick(bot: BotLike) -> str:
    try:
        return str(bot.nickname)
    except ValueError:
        return "the bot"


def _backend(bot: BotLike) -> str:
    return str(bot.getOption("backend", module="bbm")).strip().lower()


def _backend_ready(bot: BotLike) -> bool:
    if _backend(bot) == "codex":
        return bool(bot.getOption("token", module="codex_api"))
    return bool(bot.getOption("API_KEY", module="openai_api"))


def _harness_prompt(event: Event, bot: BotLike, backend: str) -> str:
    where = "a private chat" if event.isPM() else "channel %s" % event.target
    return HARNESS_PROMPT % {
        "nick": _bot_nick(bot),
        "where": where,
        "network": bot.network,
        "max_lines": max(int(bot.getOption("max_lines", module="bbm")), 1),
        "tools_note": CODEX_TOOLS_NOTE if backend == "codex" else OPENAI_TOOLS_NOTE,
        "time": strftime("%Y-%m-%d %H:%M", gmtime()),
    }


# leading "[sleep]" / "[sleep 5]" directive: the codex backend's substitute
# for the sleep tool (see CODEX_TOOLS_NOTE)
_SLEEP_DIRECTIVE = recompile(r"^\s*\[sleep(?:[ :]+(\d+(?:\.\d+)?))?\]\s*", IGNORECASE)


def _apply_sleep_directive(text: str, bot: BotLike, mute_key: tuple[str, str]) -> str:
    match = _SLEEP_DIRECTIVE.match(text)
    if not match:
        return text
    if match.group(1) is not None:
        seconds = float(match.group(1)) * 60.0
    else:
        seconds = float(bot.getOption("sleep_time", module="bbm"))
    _mute(mute_key, min(max(seconds, 30.0), 60.0 * 60.0 * 24.0))
    return text[match.end() :]


def _codex_prompt(
    bot: BotLike,
    recent: list[str],
    prior: list[openai_api.ChatMessage],
    current_line: str,
) -> str:
    """One prompt carrying everything the openai path sends as messages; the
    thread is ephemeral, so each request is self-contained."""
    parts = []
    if recent:
        parts.append("Recent messages, oldest first:\n%s" % "\n".join(recent))
    exchange = []
    for message in prior:
        if message.role == "user" and message.content:
            exchange.append(message.content)
        elif message.role == "assistant" and message.content:
            exchange.append(_format_line(_bot_nick(bot), message.content))
    if exchange:
        parts.append(
            "Your conversation with the speaker so far:\n%s" % "\n".join(exchange)
        )
    parts.append("Reply to this message:\n%s" % current_line)
    return "\n\n".join(parts)


def _deliver(bot: BotLike, text: str) -> None:
    max_lines = max(int(bot.getOption("max_lines", module="bbm")), 1)
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return
    # checkSay is None while disconnected; only an explicit False means "too long"
    if len(lines) <= max_lines and all(
        bot.checkSay(line) is not False for line in lines
    ):
        for line in lines:
            bot.say(line)
        return
    url = None
    try:
        paste = bot.getAddon("paste")
    except AttributeError:
        paste = None
    if paste is not None:
        try:
            url = paste("\n".join(lines), bot=bot, title="bbm")
        except Exception:
            log.exception("bbm paste failed")
    if url:
        for line in lines[: max_lines - 1]:
            bot.say(line)
        bot.say("full response: %s" % url)
    else:
        # no paste addon: say what fits, bot.say trims overlong lines
        for line in lines[:max_lines]:
            bot.say(line)


def _respond_openai(
    event: Event,
    bot: BotLike,
    mute_key: tuple[str, str],
    system: str,
    recent: list[str],
    prior: list[openai_api.ChatMessage],
    user_message: openai_api.ChatMessage,
) -> str:
    available = _available_tools(bot)
    specs: list[openai_api.ToolSpec] = [
        (tool.name, tool.description, tool.parameters) for tool in available.values()
    ]
    messages = [openai_api.ChatMessage(role="system", content=system)]
    if recent:
        messages.append(
            openai_api.ChatMessage(
                role="system",
                content="Recent messages, oldest first:\n%s" % "\n".join(recent),
            )
        )
    messages.extend(prior)
    messages.append(user_message)

    ctx = ToolContext(
        bot=bot, event=event, mute=lambda seconds: _mute(mute_key, seconds)
    )
    for _round in range(max(int(bot.getOption("tool_rounds", module="bbm")), 0)):
        result = _chat(bot, messages, specs)
        if not result.tool_calls:
            break
        messages.append(openai_api.assistant_reply(result))
        for call in result.tool_calls:
            messages.append(
                openai_api.ChatMessage(
                    role="tool",
                    tool_call_id=call.id,
                    content=_run_tool(available, ctx, call),
                )
            )
    else:
        # rounds exhausted (or configured to 0): force a plain text answer
        result = _chat(bot, messages, [])
    return result.content.strip()


def _respond(
    event: Event,
    bot: BotLike,
    mute_key: tuple[str, str],
    convo_key: tuple[str, str, str],
    context: list[str],
    explicit: bool,
) -> None:
    backend = _backend(bot)
    system = "\n\n".join(
        part
        for part in (
            bot.getOption("system_prompt", module="bbm"),
            _harness_prompt(event, bot, backend),
        )
        if part
    )
    context_lines = int(bot.getOption("context_lines", module="bbm"))
    recent = context[-context_lines:] if context_lines > 0 else []
    with _lock:
        conversation = _conversations.get(convo_key)
        prior = list(conversation.messages) if conversation else []
    user_message = openai_api.ChatMessage(
        role="user", content=_format_line(event.nick or "?", event.msg or "")
    )

    if backend == "codex":
        turn = codex_api.run_turn(
            bot,
            _codex_prompt(bot, recent, prior, user_message.content or ""),
            instructions=system,
        )
        text = _apply_sleep_directive(turn.text, bot, mute_key).strip()
    else:
        text = _respond_openai(
            event, bot, mute_key, system, recent, prior, user_message
        )
    if not text:
        return
    _deliver(bot, text)

    with _lock:
        conversation = _conversations.setdefault(convo_key, _Conversation())
        conversation.messages.append(user_message)
        conversation.messages.append(
            openai_api.ChatMessage(role="assistant", content=text)
        )
        del conversation.messages[:-CONVERSATION_KEEP]
        if explicit:
            conversation.remaining = int(bot.getOption("max_followups", module="bbm"))
        conversation.expires = time() + float(
            bot.getOption("followup_window", module="bbm")
        )


def heard(event: Event, bot: BotLike) -> None:
    msg, nick, target = event.msg, event.nick, event.target
    if not msg or not nick or not target:
        return
    _gc(time())
    history_target = nick if event.isPM() else target
    history = _history_for(bot.network, history_target)
    context = list(history)
    history.append(_format_line(nick, msg))

    if event.command:  # a command for some other module; context only
        return
    if any(
        isinstance(entry, str) and irc_casefold(entry) == irc_casefold(nick)
        for entry in bot.getOption("ignore", module="bbm")
    ):
        return

    mute_key = (bot.network, irc_casefold(history_target))
    now = time()
    with _lock:
        if _muted.get(mute_key, 0.0) > now:
            return
    explicit = event.isPM() or _mentioned(event, bot)
    convo_key = (*mute_key, irc_casefold(nick))
    if not explicit:
        with _lock:
            conversation = _conversations.get(convo_key)
            if conversation and conversation.expires < now:
                _conversations.pop(convo_key, None)
                conversation = None
            if conversation is None or conversation.remaining <= 0:
                return
            conversation.remaining -= 1

    if not _backend_ready(bot):
        return  # warned about at init
    _respond(event, bot, mute_key, convo_key, context, explicit)


def record_action(event: Event, bot: BotLike) -> None:
    if not event.msg or not event.nick or not event.target:
        return
    history_target = event.nick if event.isPM() else event.target
    _history_for(bot.network, history_target).append(
        "* %s %s" % (event.nick, event.msg)
    )


def record_sent(event: Event, bot: BotLike) -> None:
    if not event.target or event.msg is None:
        return
    history = _history_for(bot.network, event.target)
    for line in str(event.msg).splitlines():
        if line.strip():
            history.append(_format_line(event.nick or "me", line))


def init(bot: BotLike) -> bool:
    register_tool(bot, SLEEP_TOOL)
    for name in bot.getOption("tools", module="bbm"):
        if not isinstance(name, str) or not name.isidentifier():
            log.warning("bbm ignoring invalid ai_tools module name %r", name)
            continue
        try:
            module = import_module("pyburlybot_modules.ai_tools.%s" % name)
            for tool in module.get_tools(bot):
                register_tool(bot, tool)
        except Exception:
            log.exception("bbm could not load ai_tools module %r", name)
    if not _backend_ready(bot):
        log.warning(
            "bbm is loaded but its %r backend has no credential (openai_api "
            "API_KEY / codex_api token); mentions will be ignored.",
            _backend(bot),
        )
    return True


mappings = (
    Mapping(types=["privmsged"], function=heard),
    Mapping(types=["action"], function=record_action),
    Mapping(types=["sendmsg"], function=record_sent),
)
