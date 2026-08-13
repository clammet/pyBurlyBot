import json
from datetime import datetime
from types import SimpleNamespace
from typing import cast
from unittest import TestCase
from unittest.mock import Mock, patch

from pyburlybot_modules.gdq import (
    GDQScheduleError,
    ScheduleRun,
    gdq,
    parse_schedule_page,
    schedule_status,
)
from pyburlybot_modules.gdqdonate import (
    DONATION_API_URL,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    gdqdonate,
)
from util.event import Event
from util.http import HTTPError


def flight_script(payload: str) -> str:
    return "<script>self.__next_f.push(%s)</script>" % json.dumps([1, payload])


class GDQTest(TestCase):
    def test_parses_fragmented_nextjs_schedule_data(self) -> None:
        record = (
            "1a:"
            + json.dumps(
                {
                    "type": "speedrun",
                    "name": "Fallback Name",
                    "display_name": "Visible Name",
                    "category": "Any%",
                    "starttime": "2026-08-28T03:05:00-05:00",
                    "endtime": "2026-08-28T04:15:00-05:00",
                }
            )
            + "\n"
        )
        split_at = len(record) // 2
        page = flight_script(record[:split_at]) + flight_script(record[split_at:])

        (running,) = parse_schedule_page(page)

        self.assertEqual(running.name, "Visible Name")
        self.assertEqual(running.category, "Any%")
        offset = running.start.utcoffset()
        self.assertIsNotNone(offset)
        self.assertEqual(offset.total_seconds() if offset else None, -5 * 60 * 60)

    def test_selects_current_and_upcoming_runs(self) -> None:
        first = ScheduleRun(
            "First",
            "Any%",
            datetime.fromisoformat("2026-08-28T03:00:00-05:00"),
            datetime.fromisoformat("2026-08-28T04:00:00-05:00"),
        )
        second = ScheduleRun(
            "Second",
            "100%",
            datetime.fromisoformat("2026-08-28T04:00:00-05:00"),
            datetime.fromisoformat("2026-08-28T05:00:00-05:00"),
        )

        current, upcoming = schedule_status(
            (first, second), datetime.fromisoformat("2026-08-28T03:30:00-05:00")
        )

        self.assertIs(current, first)
        self.assertEqual(upcoming, (second,))

    def test_command_handles_schedule_failure(self) -> None:
        event = cast(Event, SimpleNamespace(argument=""))
        bot = Mock()

        failure = Mock(side_effect=GDQScheduleError("offline"))
        # Module-registry tests deliberately evict plugin modules from sys.modules,
        # so patch the imported command's globals rather than a module-name lookup.
        with patch.dict(gdq.__globals__, {"fetch_schedule": failure}):
            gdq(event, bot)

        failure.assert_called_once_with()
        bot.say.assert_called_once_with(
            "GDQ schedule is temporarily unavailable. Try again later."
        )


class GDQDonateTest(TestCase):
    def test_uses_an_accepted_user_agent_and_timeout(self) -> None:
        event = cast(Event, SimpleNamespace())
        bot = Mock()
        response = Mock(text="  donation comment  ")
        client = Mock()
        client.get.return_value = response
        client_factory = Mock(return_value=client)

        with patch.dict(gdqdonate.__globals__, {"HTTPClient": client_factory}):
            gdqdonate(event, bot)

        client_factory.assert_called_once_with(timeout=REQUEST_TIMEOUT)
        client.get.assert_called_once_with(DONATION_API_URL, headers=REQUEST_HEADERS)
        bot.say.assert_called_once_with("donation comment")

    def test_handles_donation_feed_failure(self) -> None:
        event = cast(Event, SimpleNamespace())
        bot = Mock()
        client = Mock()
        client.get.side_effect = HTTPError("offline")
        client_factory = Mock(return_value=client)

        with patch.dict(gdqdonate.__globals__, {"HTTPClient": client_factory}):
            gdqdonate(event, bot)

        bot.say.assert_called_once_with(
            "The GDQ donation feed is temporarily unavailable. Try again later."
        )
