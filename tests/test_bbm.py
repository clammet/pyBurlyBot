from typing import Any
from unittest import TestCase
from unittest.mock import patch

from pyburlybot_modules import bbm, codex_api, openai_api
from pyburlybot_modules.ai_tools import AITool, ToolContext
from util.event import Event
from util.options import option_spec


def text_result(content: str) -> openai_api.ChatResult:
    return openai_api.ChatResult(
        content=content, tool_calls=(), finish_reason="stop", model="test"
    )


def tool_result(*calls: openai_api.ToolCall) -> openai_api.ChatResult:
    return openai_api.ChatResult(
        content="", tool_calls=calls, finish_reason="tool_calls", model="test"
    )


class ScriptedChat:
    """Stands in for openai_api.chat; replays scripted results and records calls."""

    def __init__(self, results: list[openai_api.ChatResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[openai_api.ChatMessage], dict[str, Any]]] = []

    def __call__(self, bot: Any, messages: Any, **kwargs: Any) -> openai_api.ChatResult:
        self.calls.append((list(messages), kwargs))
        if not self.results:
            raise AssertionError("unexpected chat call")
        return self.results.pop(0)


class FakeBot:
    network = "testnet"
    nickname = "bbm"

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.opts: dict[str, dict[str, Any]] = {
            "bbm": _option_defaults(bbm.OPTIONS),
            "openai_api": _option_defaults(openai_api.OPTIONS),
            "codex_api": _option_defaults(codex_api.OPTIONS),
        }
        self.opts["openai_api"]["API_KEY"] = "sk-test"
        self.addons: dict[str, Any] = {}
        self.available: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def say(self, msg: Any, **kwargs: Any) -> None:
        self.messages.append(str(msg))

    def checkSay(self, msg: str) -> bool:
        return len(msg) < 400

    def getOption(self, opt: str, module: str | None = None, **kwargs: Any) -> Any:
        assert module is not None
        return self.opts[module][opt]

    def setOption(self, opt: str, value: Any, **kwargs: Any) -> None:
        raise AttributeError(opt)

    def isModuleAvailable(self, name: str) -> bool:
        return name in self.available

    def getAddon(self, name: str) -> Any:
        try:
            return self.addons[name]
        except KeyError:
            raise AttributeError(name) from None


def _option_defaults(options: dict[str, Any]) -> dict[str, Any]:
    return {name: option_spec(spec).default for name, spec in options.items()}


def channel_msg(nick: str, msg: str, command: str | None = None) -> Event:
    return Event("privmsged", target="#chan", msg=msg, nick=nick, command=command)


class BBMTest(TestCase):
    def setUp(self) -> None:
        bbm._history.clear()
        bbm._history_seen.clear()
        bbm._conversations.clear()
        bbm._muted.clear()
        bbm._tools.clear()
        bbm._gc_due["at"] = 0.0
        self.bot = FakeBot()

    def heard(self, event: Event, *results: openai_api.ChatResult) -> ScriptedChat:
        scripted = ScriptedChat(list(results))
        with patch.object(openai_api, "chat", scripted):
            bbm.heard(event, self.bot)
        return scripted

    def test_mention_triggers_reply(self) -> None:
        scripted = self.heard(
            channel_msg("alice", "bbm: hello there"), text_result("hi alice")
        )
        self.assertEqual(self.bot.messages, ["hi alice"])
        messages, kwargs = scripted.calls[0]
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[-1].content, "<alice> bbm: hello there")
        self.assertEqual(kwargs["model"], "gpt-5-mini")

    def test_unrelated_line_is_context_only(self) -> None:
        self.heard(channel_msg("bob", "the js framework drama continues"))
        self.assertEqual(self.bot.messages, [])
        scripted = self.heard(
            channel_msg("alice", "bbm what was bob talking about?"),
            text_result("summarized"),
        )
        recent = scripted.calls[0][0][1]
        self.assertEqual(recent.role, "system")
        assert recent.content is not None
        self.assertIn("<bob> the js framework drama continues", recent.content)
        # the triggering line is the user message, not part of the context block
        self.assertNotIn("what was bob talking about", recent.content)

    def test_command_lines_are_ignored(self) -> None:
        self.heard(channel_msg("alice", "!weather bbm", command="weather"))
        self.assertEqual(self.bot.messages, [])

    def test_ignored_nicks_are_ignored(self) -> None:
        self.bot.opts["bbm"]["ignore"] = ["OtherBot"]
        self.heard(channel_msg("otherbot", "bbm: hi"))
        self.assertEqual(self.bot.messages, [])

    def test_missing_api_key_stays_silent(self) -> None:
        self.bot.opts["openai_api"]["API_KEY"] = ""
        self.heard(channel_msg("alice", "bbm: hello"))
        self.assertEqual(self.bot.messages, [])

    def test_private_messages_always_trigger(self) -> None:
        event = Event("privmsged", target="bbm", msg="hello", nick="alice")
        self.heard(event, text_result("hi"))
        self.assertEqual(self.bot.messages, ["hi"])

    def test_followups_are_limited_and_keep_the_thread(self) -> None:
        self.bot.opts["bbm"]["max_followups"] = 1
        self.heard(channel_msg("alice", "bbm: hello"), text_result("hi alice"))
        scripted = self.heard(
            channel_msg("alice", "and another thing"), text_result("sure")
        )
        self.assertEqual(self.bot.messages, ["hi alice", "sure"])
        # the follow-up request replays the earlier exchange
        contents = [m.content for m in scripted.calls[0][0]]
        self.assertIn("<alice> bbm: hello", contents)
        self.assertIn("hi alice", contents)
        # budget spent: a further unaddressed line gets no reply
        self.heard(channel_msg("alice", "one more thing"))
        self.assertEqual(self.bot.messages, ["hi alice", "sure"])

    def test_followup_window_expires(self) -> None:
        self.heard(channel_msg("alice", "bbm: hello"), text_result("hi"))
        key = ("testnet", "#chan", "alice")
        bbm._conversations[key].expires = 1.0
        self.heard(channel_msg("alice", "still there?"))
        self.assertEqual(self.bot.messages, ["hi"])

    def test_other_speakers_do_not_consume_followups(self) -> None:
        self.heard(channel_msg("alice", "bbm: hello"), text_result("hi"))
        self.heard(channel_msg("bob", "unrelated chatter"))
        self.assertEqual(self.bot.messages, ["hi"])

    def test_sleep_tool_mutes_channel(self) -> None:
        bbm.register_tool(self.bot, bbm.SLEEP_TOOL)
        self.heard(
            channel_msg("alice", "bbm, shut up"),
            tool_result(
                openai_api.ToolCall(id="c1", name="sleep", arguments='{"minutes": 5}')
            ),
            text_result("fine, going quiet"),
        )
        self.assertEqual(self.bot.messages, ["fine, going quiet"])
        self.assertGreater(bbm._muted[("testnet", "#chan")], 0)
        # muted: a new mention is dropped without calling the model
        self.heard(channel_msg("alice", "bbm: you there?"))
        self.assertEqual(self.bot.messages, ["fine, going quiet"])

    def test_tool_round_trip(self) -> None:
        seen: list[dict[str, Any]] = []

        def echo(ctx: ToolContext, args: dict[str, Any]) -> str:
            seen.append(args)
            return "echo:%s" % args.get("x")

        bbm.register_tool(
            self.bot,
            AITool(
                name="echo", description="", parameters={"type": "object"}, func=echo
            ),
        )
        scripted = self.heard(
            channel_msg("alice", "bbm run echo"),
            tool_result(
                openai_api.ToolCall(id="c1", name="echo", arguments='{"x": 5}')
            ),
            text_result("done"),
        )
        self.assertEqual(seen, [{"x": 5}])
        self.assertEqual(self.bot.messages, ["done"])
        second_call_messages = scripted.calls[1][0]
        tool_messages = [m for m in second_call_messages if m.role == "tool"]
        self.assertEqual(tool_messages[0].tool_call_id, "c1")
        self.assertEqual(tool_messages[0].content, "echo:5")

    def test_tools_with_missing_requirements_are_not_advertised(self) -> None:
        def noop(ctx: ToolContext, args: dict[str, Any]) -> str:
            return ""

        bbm.register_tool(
            self.bot,
            AITool(name="plain", description="", parameters={}, func=noop),
        )
        bbm.register_tool(
            self.bot,
            AITool(
                name="needs_google",
                description="",
                parameters={},
                func=noop,
                requires=("googleapi",),
            ),
        )
        scripted = self.heard(channel_msg("alice", "bbm hi"), text_result("hi"))
        advertised = [name for name, _desc, _params in scripted.calls[0][1]["tools"]]
        self.assertEqual(advertised, ["plain"])

    def test_run_tool_reports_errors_as_strings(self) -> None:
        def boom(ctx: ToolContext, args: dict[str, Any]) -> str:
            raise RuntimeError("nope")

        tools = {"boom": AITool(name="boom", description="", parameters={}, func=boom)}
        ctx = ToolContext(
            bot=self.bot, event=channel_msg("alice", "x"), mute=lambda s: None
        )
        call = openai_api.ToolCall(id="1", name="missing", arguments="{}")
        self.assertIn("unknown tool", bbm._run_tool(tools, ctx, call))
        call = openai_api.ToolCall(id="1", name="boom", arguments="not json")
        self.assertIn("not valid JSON", bbm._run_tool(tools, ctx, call))
        call = openai_api.ToolCall(id="1", name="boom", arguments="[1]")
        self.assertIn("JSON object", bbm._run_tool(tools, ctx, call))
        call = openai_api.ToolCall(id="1", name="boom", arguments="{}")
        self.assertIn("RuntimeError: nope", bbm._run_tool(tools, ctx, call))

    def test_overflow_goes_to_paste(self) -> None:
        pastes: list[str] = []

        def paste(content: str, bot: Any = None, title: str = "") -> str:
            pastes.append(content)
            return "http://paste.test/abc"

        self.bot.addons["paste"] = paste
        self.heard(
            channel_msg("alice", "bbm essay please"),
            text_result("one\ntwo\nthree\nfour"),
        )
        self.assertEqual(
            self.bot.messages, ["one", "full response: http://paste.test/abc"]
        )
        self.assertEqual(pastes, ["one\ntwo\nthree\nfour"])

    def test_overflow_without_paste_addon_truncates(self) -> None:
        self.heard(
            channel_msg("alice", "bbm essay please"),
            text_result("one\ntwo\nthree"),
        )
        self.assertEqual(self.bot.messages, ["one", "two"])

    def test_outbound_messages_are_recorded_as_context(self) -> None:
        event = Event("sendmsg", target="#chan", msg="something I said", nick="bbm")
        bbm.record_sent(event, self.bot)
        self.assertEqual(
            list(bbm._history[("testnet", "#chan")]), ["<bbm> something I said"]
        )

    def test_actions_are_recorded_as_context(self) -> None:
        event = Event("action", target="#chan", msg="waves", nick="alice")
        bbm.record_action(event, self.bot)
        self.assertEqual(list(bbm._history[("testnet", "#chan")]), ["* alice waves"])


class ScriptedTurns:
    """Stands in for codex_api.run_turn; replays texts and records calls."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[tuple[Any, str, str | None]] = []

    def __call__(
        self, bot: Any, prompt: str, *, instructions: str | None = None
    ) -> codex_api.TurnResult:
        self.calls.append((bot, prompt, instructions))
        if not self.texts:
            raise AssertionError("unexpected codex turn")
        return codex_api.TurnResult(
            text=self.texts.pop(0), thread_id="t-1", model="gpt-5-codex"
        )


class CodexBackendTest(TestCase):
    def setUp(self) -> None:
        bbm._history.clear()
        bbm._history_seen.clear()
        bbm._conversations.clear()
        bbm._muted.clear()
        bbm._tools.clear()
        bbm._gc_due["at"] = 0.0
        self.bot = FakeBot()
        self.bot.opts["bbm"]["backend"] = "codex"
        self.bot.opts["codex_api"]["token"] = "cap-token"

    def heard(self, event: Event, *texts: str) -> ScriptedTurns:
        scripted = ScriptedTurns(list(texts))
        with patch.object(codex_api, "run_turn", scripted):
            bbm.heard(event, self.bot)
        return scripted

    def test_mention_runs_a_codex_turn(self) -> None:
        self.heard(channel_msg("bob", "the js framework drama continues"))
        scripted = self.heard(channel_msg("alice", "bbm: hello"), "hi alice")
        self.assertEqual(self.bot.messages, ["hi alice"])
        _bot, prompt, instructions = scripted.calls[0]
        self.assertIn("<bob> the js framework drama continues", prompt)
        self.assertIn("Reply to this message:\n<alice> bbm: hello", prompt)
        assert instructions is not None
        self.assertIn(bbm.DEFAULT_PERSONALITY, instructions)
        self.assertIn("[sleep N]", instructions)

    def test_missing_token_stays_silent(self) -> None:
        self.bot.opts["codex_api"]["token"] = ""
        self.heard(channel_msg("alice", "bbm: hello"))
        self.assertEqual(self.bot.messages, [])

    def test_followup_includes_prior_exchange(self) -> None:
        self.heard(channel_msg("alice", "bbm: hello"), "hi alice")
        scripted = self.heard(channel_msg("alice", "and another thing"), "sure")
        self.assertEqual(self.bot.messages, ["hi alice", "sure"])
        _bot, prompt, _instructions = scripted.calls[0]
        self.assertIn("Your conversation with the speaker so far:", prompt)
        self.assertIn("<alice> bbm: hello", prompt)
        self.assertIn("<bbm> hi alice", prompt)

    def test_sleep_directive_mutes_and_is_stripped(self) -> None:
        self.heard(channel_msg("alice", "bbm, shut up"), "[sleep 5] fine, bye")
        self.assertEqual(self.bot.messages, ["fine, bye"])
        muted_until = bbm._muted[("testnet", "#chan")]
        self.assertGreater(muted_until, bbm.time() + 250)
        self.heard(channel_msg("alice", "bbm: still there?"))
        self.assertEqual(self.bot.messages, ["fine, bye"])

    def test_bare_sleep_directive_uses_default_duration(self) -> None:
        self.bot.opts["bbm"]["sleep_time"] = 900
        self.heard(channel_msg("alice", "bbm shush"), "[sleep] ok")
        self.assertEqual(self.bot.messages, ["ok"])
        self.assertGreater(bbm._muted[("testnet", "#chan")], bbm.time() + 850)

    def test_reply_without_directive_does_not_mute(self) -> None:
        self.heard(channel_msg("alice", "bbm: hi"), "let's talk about [sleep] hygiene")
        self.assertEqual(self.bot.messages, ["let's talk about [sleep] hygiene"])
        self.assertNotIn(("testnet", "#chan"), bbm._muted)


class GCTest(TestCase):
    def setUp(self) -> None:
        bbm._history.clear()
        bbm._history_seen.clear()
        bbm._conversations.clear()
        bbm._muted.clear()
        bbm._gc_due["at"] = 0.0

    def test_gc_drops_expired_and_idle_state_only(self) -> None:
        now = 1_000_000.0
        bbm._conversations[("n", "#a", "old")] = bbm._Conversation(expires=now - 1)
        bbm._conversations[("n", "#a", "live")] = bbm._Conversation(expires=now + 100)
        bbm._muted[("n", "#old")] = now - 1
        bbm._muted[("n", "#live")] = now + 100
        for key, seen in (
            (("n", "#idle"), now - bbm.HISTORY_IDLE_SECS - 1),
            (("n", "#busy"), now - 60),
        ):
            bbm._history[key] = bbm.deque(["<x> y"], maxlen=bbm.HISTORY_MAX)
            bbm._history_seen[key] = seen

        bbm._gc(now)
        self.assertEqual(list(bbm._conversations), [("n", "#a", "live")])
        self.assertEqual(list(bbm._muted), [("n", "#live")])
        self.assertEqual(list(bbm._history), [("n", "#busy")])
        self.assertEqual(list(bbm._history_seen), [("n", "#busy")])

    def test_gc_is_time_gated(self) -> None:
        now = 1_000_000.0
        bbm._gc(now)
        bbm._conversations[("n", "#a", "old")] = bbm._Conversation(expires=now - 1)
        bbm._gc(now + 1)  # inside the interval: no sweep
        self.assertIn(("n", "#a", "old"), bbm._conversations)
        bbm._gc(now + bbm.GC_INTERVAL + 1)
        self.assertNotIn(("n", "#a", "old"), bbm._conversations)


class MentionRegexTest(TestCase):
    def test_word_boundaries_and_case(self) -> None:
        pattern = bbm._mention_regex(("bbm",))
        assert pattern is not None
        self.assertTrue(pattern.search("bbm, what's up"))
        self.assertTrue(pattern.search("what's up BBM?"))
        self.assertTrue(pattern.search("bbm: hi"))
        self.assertFalse(pattern.search("bbms are cool"))
        self.assertFalse(pattern.search("abbm said so"))

    def test_nick_with_special_characters(self) -> None:
        pattern = bbm._mention_regex(("b[b]m",))
        assert pattern is not None
        self.assertTrue(pattern.search("hey b[b]m hi"))
        self.assertFalse(pattern.search("hey bbm hi"))

    def test_no_names_yields_no_pattern(self) -> None:
        self.assertIsNone(bbm._mention_regex(()))
