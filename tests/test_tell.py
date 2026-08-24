import sqlite3
from types import SimpleNamespace
from typing import Any, cast
from unittest import TestCase

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

    def dbQuery(
        self, statement: str, params: tuple = (), function: object = None
    ) -> object:
        cursor = self.connection.execute(statement, params)
        return function(cursor) if callable(function) else cursor.fetchall()

    def getModule(self, name: str) -> Any:
        return _FakeUsers

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
        tells(_event(), cast(BotLike, self.bot))
        self.assertEqual(len(self.bot.said), 1)
        self.assertIn("tell three", self.bot.said[0])

    def test_numeric_argument_repeats_that_many_recent_batches(self) -> None:
        for argument in ("2", "-2"):
            self.bot.said.clear()
            tells(_event(argument), cast(BotLike, self.bot))
            self.assertEqual(len(self.bot.said), 3)
            self.assertIn("tell three", self.bot.said[0])
            self.assertIn("tell one", self.bot.said[1])
            self.assertIn("tell two", self.bot.said[2])

    def test_request_beyond_history_repeats_every_available_batch(self) -> None:
        tells(_event("9"), cast(BotLike, self.bot))
        self.assertEqual(len(self.bot.said), 3)

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
