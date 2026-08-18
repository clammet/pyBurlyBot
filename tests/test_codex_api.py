from json import dumps, loads
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from pyburlybot_modules import codex_api
from util.options import option_spec
from util.settings import ConfigException


def response(call_id: int, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": call_id, "result": result}


def notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


THREAD_STARTED = response(2, {"thread": {"id": "t-1"}, "model": "gpt-5-codex"})
TURN_STARTED = response(3, {"turn": {"id": "u-1", "status": "inProgress"}})


def agent_message(text: str) -> dict[str, Any]:
    return notification(
        "item/completed",
        {
            "threadId": "t-1",
            "turnId": "u-1",
            "completedAtMs": 0,
            "item": {"type": "agentMessage", "id": "i-1", "text": text},
        },
    )


def turn_completed(
    status: str = "completed", error: str | None = None
) -> dict[str, Any]:
    turn: dict[str, Any] = {"id": "u-1", "status": status}
    if error:
        turn["error"] = {"message": error}
    return notification("turn/completed", {"threadId": "t-1", "turn": turn})


class FakeWS:
    def __init__(self, incoming: list[dict[str, Any]]) -> None:
        self.incoming = list(incoming)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def send_text(self, text: str) -> None:
        self.sent.append(loads(text))

    def recv_text(self) -> str:
        if not self.incoming:
            raise AssertionError("client read more messages than scripted")
        return dumps(self.incoming.pop(0))

    def settimeout(self, timeout: float | None) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeWS":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _option_defaults(options: dict[str, Any]) -> dict[str, Any]:
    return {name: option_spec(spec).default for name, spec in options.items()}


class FakeBot:
    network = "testnet"

    def __init__(self) -> None:
        self.opts = _option_defaults(codex_api.OPTIONS)
        self.opts["token"] = "cap-token"

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def say(self, msg: Any, **kwargs: Any) -> None:
        raise AttributeError("say")

    def getOption(self, opt: str, module: str | None = None, **kwargs: Any) -> Any:
        assert module == "codex_api"
        return self.opts[opt]

    def setOption(self, opt: str, value: Any, **kwargs: Any) -> None:
        raise AttributeError(opt)


class CodexAPITest(TestCase):
    def run_turn(
        self, incoming: list[dict[str, Any]], bot: FakeBot | None = None, **kwargs: Any
    ) -> tuple[codex_api.TurnResult, FakeWS, dict[str, Any]]:
        ws = FakeWS(incoming)
        captured: dict[str, Any] = {}

        def fake_client(url: str, **client_kwargs: Any) -> FakeWS:
            captured["url"] = url
            captured.update(client_kwargs)
            return ws

        with patch.object(codex_api, "WebSocketClient", fake_client):
            result = codex_api.run_turn(bot or FakeBot(), "hello there", **kwargs)
        return result, ws, captured

    def test_happy_path_runs_the_protocol(self) -> None:
        incoming = [
            response(1, {}),
            THREAD_STARTED,
            TURN_STARTED,
            notification("item/agentMessage/delta", {"delta": "ig"}),
            agent_message("hi alice"),
            turn_completed(),
        ]
        result, ws, captured = self.run_turn(incoming, instructions="be brief")
        self.assertEqual(result.text, "hi alice")
        self.assertEqual(result.thread_id, "t-1")
        self.assertEqual(result.model, "gpt-5-codex")
        self.assertEqual(captured["url"], "ws://codex:4500")
        self.assertEqual(captured["headers"], {"Authorization": "Bearer cap-token"})
        self.assertTrue(ws.closed)

        init, thread_start, turn_start = ws.sent
        self.assertEqual(init["method"], "initialize")
        self.assertEqual(init["params"]["clientInfo"]["name"], "pyburlybot")
        self.assertEqual(thread_start["method"], "thread/start")
        self.assertEqual(
            thread_start["params"],
            {
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "developerInstructions": "be brief",
            },
        )
        self.assertEqual(turn_start["method"], "turn/start")
        self.assertEqual(
            turn_start["params"],
            {
                "threadId": "t-1",
                "input": [{"type": "text", "text": "hello there"}],
                "effort": "low",
            },
        )

    def test_multiple_agent_messages_are_joined(self) -> None:
        incoming = [
            response(1, {}),
            THREAD_STARTED,
            TURN_STARTED,
            agent_message("one"),
            agent_message("two"),
            turn_completed(),
        ]
        result, _ws, _captured = self.run_turn(incoming)
        self.assertEqual(result.text, "one\ntwo")

    def test_terminal_error_notification_raises(self) -> None:
        incoming = [
            response(1, {}),
            THREAD_STARTED,
            TURN_STARTED,
            notification(
                "error",
                {
                    "threadId": "t-1",
                    "turnId": "u-1",
                    "willRetry": True,
                    "error": {"message": "Reconnecting... 2/5"},
                },
            ),
            notification(
                "error",
                {
                    "threadId": "t-1",
                    "turnId": "u-1",
                    "willRetry": False,
                    "error": {"message": "stream disconnected (401)"},
                },
            ),
        ]
        with self.assertRaisesRegex(codex_api.CodexTurnError, "401"):
            self.run_turn(incoming)

    def test_failed_turn_raises_with_detail(self) -> None:
        incoming = [
            response(1, {}),
            THREAD_STARTED,
            TURN_STARTED,
            turn_completed(status="failed", error="usage limit reached"),
        ]
        with self.assertRaisesRegex(codex_api.CodexTurnError, "usage limit"):
            self.run_turn(incoming)

    def test_jsonrpc_error_response_raises(self) -> None:
        incoming = [
            response(1, {}),
            {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32600, "message": "bad params"},
            },
        ]
        with self.assertRaisesRegex(
            codex_api.CodexTurnError, "thread/start.*bad params"
        ):
            self.run_turn(incoming)

    def test_missing_token_never_connects(self) -> None:
        bot = FakeBot()
        bot.opts["token"] = ""

        def explode(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("must not connect without a token")

        with patch.object(codex_api, "WebSocketClient", explode):
            with self.assertRaises(ConfigException):
                codex_api.run_turn(bot, "hi")

    def test_empty_effort_and_custom_model_are_forwarded(self) -> None:
        bot = FakeBot()
        bot.opts["effort"] = ""
        bot.opts["model"] = "gpt-5.1-codex-mini"
        incoming = [
            response(1, {}),
            THREAD_STARTED,
            TURN_STARTED,
            agent_message("ok"),
            turn_completed(),
        ]
        _result, ws, _captured = self.run_turn(incoming, bot=bot)
        self.assertEqual(ws.sent[1]["params"]["model"], "gpt-5.1-codex-mini")
        self.assertNotIn("effort", ws.sent[2]["params"])
