import sqlite3
from typing import Any, cast
from unittest import TestCase

from pyburlybot_modules.alert import deliver_alerts
from util.types import BotLike


class _BackgroundBot:
    def __init__(self, alerts: list[sqlite3.Row]) -> None:
        self.alerts = alerts
        self.sent: list[tuple[str, str]] = []

    def dbBatch(self, queries: tuple[Any, ...]) -> list[list[sqlite3.Row]]:
        return [[alert] for alert in self.alerts]

    def getAddon(self, name: str) -> Any:
        if name != "paste":
            raise AttributeError(name)
        return lambda content, **kwargs: "https://paste.example/alerts"

    def sendmsg(self, target: str, msg: str) -> None:
        self.sent.append((target, msg))

    def say(self, msg: str, **kwargs: Any) -> None:
        raise AssertionError("background callbacks have no reply source")


class AlertDeliveryTest(TestCase):
    def test_collated_alerts_send_paste_to_stored_source(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """CREATE TABLE alert(
                id INTEGER PRIMARY KEY, target_user TEXT, alert_time INTEGER,
                created_time INTEGER, source TEXT, source_user TEXT, msg TEXT);"""
        )
        connection.executemany(
            """INSERT INTO alert
                VALUES (?, 'alice', ?, ?, '#alerts', NULL, ?);""",
            (
                (alert_id, alert_id, alert_id, "message %d" % alert_id)
                for alert_id in range(1, 5)
            ),
        )
        alerts = connection.execute("SELECT * FROM alert ORDER BY id;").fetchall()
        bot = _BackgroundBot(alerts)

        deliver_alerts("#alerts", alerts, cast(BotLike, bot))

        self.assertEqual(
            bot.sent,
            [("#alerts", "Alerts for (alice): https://paste.example/alerts")],
        )
