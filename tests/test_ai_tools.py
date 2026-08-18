from typing import Any
from unittest import TestCase

from pyburlybot_modules.ai_tools import ToolContext, calculator, weather, websearch
from util.event import Event


class FakeToolBot:
    network = "testnet"

    def __init__(self, modules: dict[str, Any] | None = None) -> None:
        self.modules = modules or {}

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def say(self, msg: Any, **kwargs: Any) -> None:
        raise AttributeError("tools must not say() directly")

    def getOption(self, opt: str, **kwargs: Any) -> Any:
        raise AttributeError(opt)

    def setOption(self, opt: str, value: Any, **kwargs: Any) -> None:
        raise AttributeError(opt)

    def getModule(self, name: str) -> Any:
        return self.modules[name]

    def dbQuery(self, *args: Any, **kwargs: Any) -> Any:
        return None


def make_ctx(bot: FakeToolBot, nick: str = "alice") -> ToolContext:
    event = Event("privmsged", target="#chan", msg="hi", nick=nick)
    return ToolContext(bot=bot, event=event, mute=lambda seconds: None)


class CalculatorTest(TestCase):
    def test_arithmetic(self) -> None:
        self.assertEqual(calculator.evaluate("3475 * 786324 / 3"), 910825300.0)
        self.assertEqual(calculator.evaluate("(2 + 3) ** 2"), 25)
        self.assertEqual(calculator.evaluate("-7 // 2"), -4)
        self.assertEqual(calculator.evaluate("10 % 3"), 1)

    def test_formatting(self) -> None:
        self.assertEqual(calculator.format_result(910825300.0), "910825300")
        self.assertEqual(calculator.format_result(0.5), "0.5")
        self.assertEqual(calculator.format_result(25), "25")

    def test_rejects_non_arithmetic(self) -> None:
        for expression in (
            "__import__('os').system('true')",
            "a + 1",
            "(1).bit_length()",
            "True + 1",
            "'x' * 3",
            "1 << 64",
        ):
            with self.assertRaises(ValueError):
                calculator.evaluate(expression)

    def test_bounds_hostile_expressions(self) -> None:
        with self.assertRaises(ValueError):
            calculator.evaluate("9 ** 9 ** 9")
        with self.assertRaises(ValueError):
            calculator.evaluate("(10 ** 100) ** 500")

    def test_tool_reports_errors_as_strings(self) -> None:
        (tool,) = calculator.get_tools(FakeToolBot())
        ctx = make_ctx(FakeToolBot())
        self.assertEqual(
            tool.func(ctx, {"expression": "3475 * 786324 / 3"}),
            "3475 * 786324 / 3 = 910825300",
        )
        self.assertIn("Error", tool.func(ctx, {"expression": "1 / 0"}))
        self.assertIn("Error", tool.func(ctx, {}))


class FakeGoogleAPI:
    def __init__(
        self, results: list[tuple[str, str, str]], spelling: str | None = None
    ) -> None:
        self.results = results
        self.spelling = spelling
        self.queries: list[tuple[str, int]] = []

    def google(
        self, bot: Any, query: str, num_results: int = 1
    ) -> tuple[str | None, list[tuple[str, str, str]]]:
        self.queries.append((query, num_results))
        return self.spelling, self.results


class WebSearchTest(TestCase):
    def test_formats_results(self) -> None:
        api = FakeGoogleAPI([("Title", "Snippet.", "http://example.test")])
        bot = FakeToolBot({"googleapi": api})
        (tool,) = websearch.get_tools(bot)
        result = tool.func(make_ctx(bot), {"query": "example", "count": 2})
        self.assertEqual(result, "Title: Snippet. (http://example.test)")
        self.assertEqual(api.queries, [("example", 2)])

    def test_reports_no_results_and_spelling(self) -> None:
        api = FakeGoogleAPI([], spelling="example")
        bot = FakeToolBot({"googleapi": api})
        (tool,) = websearch.get_tools(bot)
        self.assertEqual(
            tool.func(make_ctx(bot), {"query": "exampel"}),
            "No results. Did you mean: example",
        )
        self.assertIn("Error", tool.func(make_ctx(bot), {}))


class FakeLocationModule:
    def __init__(self, saved: Any = None, lookup: Any = None) -> None:
        self.saved = saved
        self.lookup = lookup

    def getlocation(self, qfunc: Any, user: str) -> Any:
        return self.saved

    def lookup_location(self, bot: Any, query: str) -> Any:
        return self.lookup


class FakeUsersModule:
    def get_username(self, bot: Any, nick: str, source: Any = None) -> str:
        return nick


class FakeOWMModule:
    def get_weather(self, bot: Any, lat: Any, lon: Any) -> dict[str, Any]:
        return {
            "weather": [{"description": "light rain"}],
            "main": {"temp": 10.0, "feels_like": 7.0, "humidity": 80},
            "wind": {"speed": 5.0},
        }


class WeatherToolTest(TestCase):
    def _bot(self, location_module: FakeLocationModule) -> FakeToolBot:
        return FakeToolBot(
            {
                "location": location_module,
                "users": FakeUsersModule(),
                "openweathermap_api": FakeOWMModule(),
            }
        )

    def test_saved_location(self) -> None:
        bot = self._bot(FakeLocationModule(saved=("Lansing, MI", 42.7, -84.5)))
        (tool,) = weather.get_tools(bot)
        result = tool.func(make_ctx(bot), {})
        self.assertIn("Weather for Lansing, MI:", result)
        self.assertIn("light rain", result)
        self.assertIn("10.0C/50.0F", result)
        self.assertIn("feels like 7.0C/44.6F", result)

    def test_unknown_user_location_asks_for_one(self) -> None:
        bot = self._bot(FakeLocationModule(saved=None))
        (tool,) = weather.get_tools(bot)
        result = tool.func(make_ctx(bot), {})
        self.assertIn("No saved location for alice", result)
        self.assertIn("Ask them where they are", result)

    def test_explicit_place(self) -> None:
        bot = self._bot(FakeLocationModule(lookup=("Tokyo, Japan", 35.7, 139.7)))
        (tool,) = weather.get_tools(bot)
        result = tool.func(make_ctx(bot), {"location": "tokyo"})
        self.assertIn("Weather for Tokyo, Japan:", result)

    def test_unknown_place(self) -> None:
        bot = self._bot(FakeLocationModule(lookup=None))
        (tool,) = weather.get_tools(bot)
        self.assertIn("Error", tool.func(make_ctx(bot), {"location": "nowhereton"}))
