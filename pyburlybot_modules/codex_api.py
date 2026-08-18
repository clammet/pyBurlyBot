"""Codex app-server helper.

Owns the WebSocket connection, capability token, and error reporting for a
`codex app-server` service (the official way to use a ChatGPT subscription)
and returns typed structures, never raw JSON. One connection is opened per
turn: an ephemeral thread is started, one turn is run to completion, and the
connection is closed - so a recreated codex container (image updates) can
only ever fail the request in flight, and no server-side thread history
accumulates on bbm's behalf.

Protocol per `codex app-server generate-json-schema` (JSON-RPC 2.0 over ws):
initialize -> thread/start -> turn/start, then notifications until
turn/completed; agentMessage item/completed notifications carry the reply
text. The capability token authenticates the upgrade as a Bearer credential.
"""

from dataclasses import dataclass
from json import JSONDecodeError, dumps, loads
from time import monotonic
from typing import Any

from util import Option
from util.settings import ConfigException
from util.types import BotLike
from util.ws import WebSocketClient


OPTIONS = {
    "url": (str, "WebSocket URL of the codex app-server.", "ws://codex:4500"),
    "token": Option(
        str,
        "Capability token sent as the Bearer credential on the upgrade.",
        "",
        secret=True,
        writeonly=True,
    ),
    "model": (str, "Model requested for turns; empty uses the server default.", ""),
    "effort": (
        str,
        "Reasoning effort for turns (minimal/low/medium/high); empty uses "
        "the server default.",
        "low",
    ),
    "timeout": (int, "Seconds to wait for a turn to complete.", 120),
}

CLIENT_INFO = {"name": "pyburlybot", "version": "3"}


@dataclass(frozen=True, slots=True)
class TurnResult:
    text: str
    thread_id: str
    model: str


class CodexTurnError(RuntimeError):
    pass


class _Connection:
    """One JSON-RPC exchange; responses to our calls plus queued notifications."""

    def __init__(self, ws: WebSocketClient) -> None:
        self.ws = ws
        self.pending: list[dict[str, Any]] = []
        self.next_id = 0

    def _receive(self) -> dict[str, Any]:
        try:
            message = loads(self.ws.recv_text())
        except JSONDecodeError as exc:
            raise CodexTurnError("codex sent invalid JSON") from exc
        if not isinstance(message, dict):
            raise CodexTurnError("codex sent a non-object message")
        return message

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        call_id = self.next_id
        self.ws.send_text(
            dumps({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params})
        )
        while True:
            message = self._receive()
            if message.get("id") != call_id:
                # a notification (or a server-initiated request, which our
                # approvalPolicy=never turns should not produce): queue it for
                # the turn loop
                self.pending.append(message)
                continue
            if "error" in message:
                error = message["error"] or {}
                raise CodexTurnError(
                    "codex %s failed: %s" % (method, error.get("message", error))
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def next_notification(self) -> dict[str, Any]:
        if self.pending:
            return self.pending.pop(0)
        return self._receive()


def _drain_turn(connection: _Connection, deadline: float) -> tuple[str, str]:
    """Collect agentMessage text until the turn completes; returns (text, status)."""
    texts: list[str] = []
    last_error = ""
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CodexTurnError("codex turn timed out")
        connection.ws.settimeout(remaining)
        try:
            message = connection.next_notification()
        except TimeoutError as exc:
            raise CodexTurnError("codex turn timed out") from exc
        method = message.get("method")
        params = message.get("params") or {}
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and item.get("text"):
                texts.append(str(item["text"]))
        elif method == "error":
            error = params.get("error") or {}
            last_error = str(error.get("message") or error)
            if not params.get("willRetry"):
                raise CodexTurnError("codex turn failed: %s" % last_error)
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            status = str(turn.get("status") or "")
            if status != "completed":
                turn_error = turn.get("error") or {}
                detail = (
                    turn_error.get("message") if isinstance(turn_error, dict) else None
                )
                raise CodexTurnError(
                    "codex turn %s: %s"
                    % (status or "failed", detail or last_error or "no detail")
                )
            return "\n".join(texts), status
        # everything else (deltas, reasoning, status changes) is ignored


def run_turn(
    bot: BotLike, prompt: str, *, instructions: str | None = None
) -> TurnResult:
    """Run one prompt through a fresh ephemeral codex thread and return the
    reply. instructions become the thread's developer instructions (the
    closest thing to a system prompt the codex backend offers)."""
    token = bot.getOption("token", module="codex_api")
    if not token:
        raise ConfigException("Require token for codex_api.")
    url = str(bot.getOption("url", module="codex_api"))
    timeout = float(bot.getOption("timeout", module="codex_api"))
    model = str(bot.getOption("model", module="codex_api")).strip()
    effort = str(bot.getOption("effort", module="codex_api")).strip()

    deadline = monotonic() + timeout
    with WebSocketClient(
        url,
        headers={"Authorization": "Bearer %s" % token},
        timeout=min(timeout, 10.0),
    ) as ws:
        connection = _Connection(ws)
        ws.settimeout(timeout)
        connection.call("initialize", {"clientInfo": dict(CLIENT_INFO)})
        thread_params: dict[str, Any] = {
            # ephemeral: nothing is persisted server-side for this thread
            "ephemeral": True,
            # the container is the boundary; the model must not run commands
            "approvalPolicy": "never",
            "sandbox": "read-only",
        }
        if instructions:
            thread_params["developerInstructions"] = instructions
        if model:
            thread_params["model"] = model
        started = connection.call("thread/start", thread_params)
        thread = started.get("thread") or {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise CodexTurnError("codex thread/start returned no thread id")

        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if effort:
            turn_params["effort"] = effort
        connection.call("turn/start", turn_params)
        text, _status = _drain_turn(connection, deadline)
        return TurnResult(
            text=text,
            thread_id=thread_id,
            model=str(started.get("model") or model),
        )


def init(bot: BotLike) -> bool:
    return True
