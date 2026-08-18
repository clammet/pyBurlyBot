from typing import Any, cast
from unittest import TestCase
from unittest.mock import patch

from pyburlybot_modules import openai_api
from util.http import InvalidResponseError, Response
from util.types import BotLike


BOT = cast(BotLike, object())


def ok_response(content: str = "hi") -> dict[str, Any]:
    return {
        "model": "gpt-test",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    }


class ChatPayloadTest(TestCase):
    def run_chat(self, **kwargs: Any) -> tuple[dict[str, Any], openai_api.ChatResult]:
        captured: dict[str, Any] = {}
        response = kwargs.pop("_response", ok_response())

        def fake_post(bot: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            captured["path"] = path
            captured["payload"] = payload
            return response

        with patch.object(openai_api, "_post", fake_post):
            result = openai_api.chat(BOT, kwargs.pop("messages"), **kwargs)
        return captured, result

    def test_minimal_payload_omits_optional_parameters(self) -> None:
        captured, result = self.run_chat(
            messages=[openai_api.ChatMessage(role="user", content="hi")],
            model="gpt-test",
        )
        self.assertEqual(captured["path"], "/chat/completions")
        payload = captured["payload"]
        self.assertEqual(payload["model"], "gpt-test")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hi"}])
        for absent in ("temperature", "max_completion_tokens", "tools"):
            self.assertNotIn(absent, payload)
        self.assertEqual(result.content, "hi")
        self.assertEqual(result.finish_reason, "stop")

    def test_full_payload_wire_format(self) -> None:
        call = openai_api.ToolCall(id="c1", name="echo", arguments='{"x": 1}')
        captured, _result = self.run_chat(
            messages=[
                openai_api.ChatMessage(role="system", content="sys"),
                openai_api.ChatMessage(role="assistant", tool_calls=(call,)),
                openai_api.ChatMessage(role="tool", tool_call_id="c1", content="1"),
            ],
            model="gpt-test",
            tools=[("echo", "echoes", {"type": "object"})],
            temperature=0.5,
            max_completion_tokens=100,
            extra_params={"reasoning_effort": "low"},
        )
        payload = captured["payload"]
        self.assertEqual(payload["temperature"], 0.5)
        self.assertEqual(payload["max_completion_tokens"], 100)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(
            payload["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "echoes",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
        self.assertEqual(
            payload["messages"][1],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"x": 1}'},
                    }
                ],
            },
        )
        self.assertEqual(
            payload["messages"][2],
            {"role": "tool", "content": "1", "tool_call_id": "c1"},
        )

    def test_parses_tool_calls(self) -> None:
        response = {
            "model": "gpt-test",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c9",
                                "type": "function",
                                "function": {
                                    "name": "calculate",
                                    "arguments": '{"expression": "1+1"}',
                                },
                            }
                        ],
                    },
                }
            ],
        }
        _captured, result = self.run_chat(
            messages=[openai_api.ChatMessage(role="user", content="hi")],
            model="gpt-test",
            _response=response,
        )
        self.assertEqual(result.content, "")
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(
            result.tool_calls,
            (
                openai_api.ToolCall(
                    id="c9", name="calculate", arguments='{"expression": "1+1"}'
                ),
            ),
        )

    def test_response_without_choices_raises(self) -> None:
        with patch.object(openai_api, "_post", lambda bot, path, payload: {}):
            with self.assertRaises(InvalidResponseError):
                openai_api.chat(
                    BOT,
                    [openai_api.ChatMessage(role="user", content="hi")],
                    model="gpt-test",
                )


class ErrorDetailTest(TestCase):
    def response(self, body: bytes) -> Response:
        return Response(
            url="https://api.openai.test/v1/chat/completions",
            status=401,
            reason="Unauthorized",
            headers={},
            body=body,
        )

    def test_extracts_api_error_message(self) -> None:
        body = b'{"error": {"message": "Incorrect API key provided."}}'
        self.assertEqual(
            openai_api._error_detail(self.response(body)),
            "Incorrect API key provided.",
        )

    def test_falls_back_to_http_reason(self) -> None:
        self.assertEqual(
            openai_api._error_detail(self.response(b"not json")), "Unauthorized"
        )
