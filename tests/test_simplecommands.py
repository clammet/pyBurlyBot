from unittest import TestCase
from typing import Any

from pyburlybot_modules import simplecommands
from util.event import Event


class FakeBot:
    network = "test-network"

    def __init__(self, commands: list, limit: int = 2, window: int = 60) -> None:
        self.options = {
            "commands": commands,
            "mutation_limit": limit,
            "mutation_window": window,
        }

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def getOption(self, opt: str, **kwargs: Any) -> Any:
        return self.options[opt]

    def setOption(self, opt: str, value: Any, **kwargs: Any) -> None:
        self.options[opt] = value

    def say(self, msg: Any, **kwargs: Any) -> None:
        return None


class SimpleCommandsTest(TestCase):
    def setUp(self) -> None:
        simplecommands._rate_events.clear()

    def test_dynamic_mappings_are_built_per_server(self) -> None:
        first = FakeBot([[["one"], "first"]])
        second = FakeBot([[["two"], "second"]])

        first_commands = {
            command
            for mapping in simplecommands.get_mappings(first)
            for command in mapping.command or ()
        }
        second_commands = {
            command
            for mapping in simplecommands.get_mappings(second)
            for command in mapping.command or ()
        }
        self.assertIn("one", first_commands)
        self.assertNotIn("one", second_commands)
        self.assertIn("two", second_commands)

    def test_mutation_rate_limit_uses_authenticated_identity(self) -> None:
        bot = FakeBot([], limit=2)
        event = Event(
            "privmsged",
            nick="VisibleNick",
            account="stable-account",
            hostmask="VisibleNick!ident@host",
        )
        self.assertTrue(simplecommands._rate_limit(event, bot))
        self.assertTrue(simplecommands._rate_limit(event, bot))
        self.assertFalse(simplecommands._rate_limit(event, bot))
