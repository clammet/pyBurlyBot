import sqlite3
from types import SimpleNamespace
from typing import Any, cast
from unittest import TestCase
from unittest.mock import patch

from pyburlybot_modules.tell import tells
from util.event import Event
from util.types import BotLike


class _FakeUsers:
    @staticmethod
    def resolve_nick(bot: Any, nick: Any) -> str:
        return "alice"


class _FakeBot:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.said: list[str] = []
        self.pasted: list[str] = []

    def dbQuery(
        self, statement: str, params: tuple = (), function: object = None
    ) -> object:
        cursor = self.connection.execute(statement, params)
        return function(cursor) if callable(function) else cursor.fetchall()

    def getModule(self, name: str) -> Any:
        return _FakeUsers

    def getAddon(self, name: str) -> Any:
        if name != "paste":
            raise AttributeError(name)

        def paste(content: str, **kwargs: Any) -> str:
            self.pasted.append(content)
            return "https://paste.example/tells"

        return paste

    def say(self, msg: str, **kwargs: Any) -> None:
        strins = kwargs.get("strins")
        self.said.append(msg.format(*strins) if strins else msg)


def _event(argument: str | None = None) -> Event:
    return cast(Event, SimpleNamespace(argument=argument, nick="alice"))


class TellsTest(TestCase):
    def setUp(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(
            """CREATE TABLE tell(
                id INTEGER PRIMARY KEY, delivered INTEGER DEFAULT 0,
                user TEXT COLLATE NOCASE, telltime INTEGER, origintime INTEGER,
                toldtime INTEGER, remind INTEGER DEFAULT 0, source TEXT,
                msg TEXT);"""
        )
        rows = (
            (1, "alice", 900, 1000, "bob", "tell one"),
            (1, "alice", 905, 1000, "bob", "tell two"),
            (1, "alice", 1900, 2000, "bob", "tell three"),
            (0, "alice", 3000, None, "bob", "still pending"),
        )
        connection.executemany(
            """INSERT INTO tell(delivered, user, telltime, toldtime, source, msg)
                VALUES (?,?,?,?,?,?);""",
            rows,
        )
        self.bot = _FakeBot(connection)

    def test_repeats_the_most_recent_delivered_batch(self) -> None:
        # Patch the function's globals so this remains reliable if another test
        # reloads the tell module after this test module was imported.
        with patch.dict(tells.__globals__, {"timegm": lambda _: 2100}):
            tells(_event(), cast(BotLike, self.bot))

        self.assertEqual(
            self.bot.said,
            ["alice: <bob> tell three - 3 minutes and 20 seconds ago"],
        )

    def test_numeric_argument_repeats_that_many_recent_batches(self) -> None:
        for argument in ("2", "-2"):
            self.bot.said.clear()
            tells(_event(argument), cast(BotLike, self.bot))
            self.assertEqual(len(self.bot.said), 3)
            self.assertIn("alice: [1] <bob> tell three", self.bot.said[0])
            self.assertIn("alice: [2] <bob> tell one", self.bot.said[1])
            self.assertIn("alice: [2] <bob> tell two", self.bot.said[2])

    def test_request_beyond_history_repeats_every_available_batch(self) -> None:
        tells(_event("9"), cast(BotLike, self.bot))
        self.assertEqual(len(self.bot.said), 3)

    def test_combined_replay_over_inline_limit_uses_one_paste(self) -> None:
        self.bot.connection.execute(
            """INSERT INTO tell(
                delivered, user, telltime, toldtime, source, msg
            ) VALUES (1, 'alice', 400, 500, 'bob', 'tell four');"""
        )

        tells(_event("3"), cast(BotLike, self.bot))

        self.assertEqual(
            self.bot.said,
            ["Tells/reminds for (alice): https://paste.example/tells"],
        )
        self.assertEqual(len(self.bot.pasted), 1)
        for message in ("tell one", "tell two", "tell three", "tell four"):
            self.assertIn(message, self.bot.pasted[0])
        self.assertEqual(self.bot.pasted[0].count("[1]"), 1)
        self.assertEqual(self.bot.pasted[0].count("[2]"), 2)
        self.assertEqual(self.bot.pasted[0].count("[3]"), 1)

    def test_request_is_limited_to_ten_batches(self) -> None:
        tells(_event("11"), cast(BotLike, self.bot))
        self.assertEqual(
            self.bot.said,
            ["Can only replay up to 10 tell batches at once."],
        )

    def test_undelivered_tells_are_not_replayed(self) -> None:
        for argument in (None, "2", "3"):
            self.bot.said.clear()
            tells(_event(argument), cast(BotLike, self.bot))
            self.assertNotIn("still pending", "".join(self.bot.said))
