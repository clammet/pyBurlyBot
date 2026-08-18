"""OpenAI Chat Completions helper.

Owns the HTTP call, API-key handling, and error reporting for the OpenAI API
(or any compatible service selected with api_base) and returns typed
structures, never raw JSON dicts. Consumers build ChatMessage sequences and
receive a ChatResult; tool calls requested by the model come back as ToolCall
entries to execute and answer with tool-role messages.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from json import dumps
from typing import Any

from util import Option
from util.http import HTTPClient, InvalidResponseError, Response
from util.settings import ConfigException
from util.types import BotLike


OPTIONS = {
    "API_KEY": Option(
        str,
        "API key for the OpenAI (or compatible) service.",
        "",
        secret=True,
        writeonly=True,
    ),
    "api_base": (
        str,
        "Base URL of the OpenAI-compatible API.",
        "https://api.openai.com/v1",
    ),
    "timeout": (
        int,
        "Seconds to wait for a chat completion; tool-using requests can be slow.",
        90,
    ),
}

# (name, description, JSON Schema parameters object) advertised to the model
ToolSpec = tuple[str, str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One function call requested by the model; arguments is raw JSON text."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One conversation message. role is system, user, assistant, or tool;
    tool_calls only appears on assistant messages and tool_call_id only on
    tool-result messages."""

    role: str
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    model: str


def assistant_reply(result: ChatResult) -> ChatMessage:
    """The assistant message to echo back before answering its tool calls."""
    return ChatMessage(
        role="assistant",
        content=result.content or None,
        tool_calls=result.tool_calls,
    )


def _wire_message(message: ChatMessage) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id:
        wire["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return wire


def _error_detail(response: Response) -> str:
    try:
        data = response.json()
    except InvalidResponseError:
        return response.reason
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return response.reason


def _post(bot: BotLike, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = bot.getOption("API_KEY", module="openai_api")
    if not key:
        raise ConfigException("Require API_KEY for openai_api.")
    base = str(bot.getOption("api_base", module="openai_api")).rstrip("/")
    client = HTTPClient(timeout=float(bot.getOption("timeout", module="openai_api")))
    response = client.request(
        "POST",
        base + path,
        headers={
            "Authorization": "Bearer %s" % key,
            "Content-Type": "application/json",
        },
        body=dumps(payload).encode("utf-8"),
        raise_for_status=False,
    )
    if not 200 <= response.status < 300:
        raise RuntimeError(
            "OpenAI API error (HTTP %d): %s"
            % (response.status, _error_detail(response))
        )
    data = response.json()
    if not isinstance(data, dict):
        raise InvalidResponseError("OpenAI returned a non-object response.")
    return data


def chat(
    bot: BotLike,
    messages: Sequence[ChatMessage],
    *,
    model: str,
    tools: Sequence[ToolSpec] = (),
    temperature: float | None = None,
    max_completion_tokens: int | None = None,
    extra_params: dict[str, Any] | None = None,
) -> ChatResult:
    """Run one Chat Completions request and return the model's turn."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [_wire_message(message) for message in messages],
    }
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            }
            for name, description, parameters in tools
        ]
    if temperature is not None:
        payload["temperature"] = temperature
    if max_completion_tokens:
        payload["max_completion_tokens"] = max_completion_tokens
    if extra_params:
        payload.update(extra_params)

    data = _post(bot, "/chat/completions", payload)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise InvalidResponseError("OpenAI response has no choices.")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise InvalidResponseError("OpenAI response has no message.")
    calls = []
    raw_calls = message.get("tool_calls")
    for item in raw_calls if isinstance(raw_calls, list) else ():
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        calls.append(
            ToolCall(
                id=str(item.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=str(function.get("arguments") or "{}"),
            )
        )
    return ChatResult(
        content=str(message.get("content") or ""),
        tool_calls=tuple(calls),
        finish_reason=str(choice.get("finish_reason") or ""),
        model=str(data.get("model") or model),
    )


def init(bot: BotLike) -> bool:
    return True
