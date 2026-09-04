import json
import sqlite3
from datetime import datetime
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from pyburlybot_modules import when, whenapi
from util.event import Event
from util.http import HTTPError, Response

SCHEDULE_ID = "kn7ffashxx3pspb2qfz4kfwny58dc21t"
URL = "https://when.nyanya.org/schedule/" + SCHEDULE_ID
LINK = whenapi.ScheduleLink(SCHEDULE_ID, URL)


class FakeBot:
    network = "test"

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.messages: list[str] = []

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def getOption(self, opt: str, **kwargs: Any) -> Any:
        return (when.OPTIONS if kwargs.get("module") == "when" else whenapi.OPTIONS)[
            opt
        ][2]

    def setOption(self, opt: str, value: Any, **kwargs: Any) -> None:
        raise NotImplementedError

    def getModule(self, name: str) -> Any:
        assert name == "whenapi"
        return whenapi

    def dbCheckCreateTable(self, name: str, sql: str) -> None:
        self.db.execute(sql.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))

    def dbQuery(self, sql: str, params: tuple = (), func: Any = None) -> Any:
        cursor = self.db.execute(sql, params)
        self.db.commit()
        return func(cursor) if func else None

    def say(self, msg: str, **kwargs: Any) -> None:
        self.messages.append(msg)

    def checkSay(self, msg: str) -> bool:
        return True


def selection(profile: str = "a", **kwargs: Any) -> dict[str, Any]:
    return {
        "profileId": profile,
        "dayKey": "5",
        "timeSlot": "10:00",
        "timezone": "UTC",
        "state": "can-do",
        **kwargs,
    }


def schedule(*rows: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return {
        "_id": SCHEDULE_ID,
        "type": "recurring",
        "creatorTimezone": "UTC",
        "profiles": [
            {"_id": "a", "displayName": "Alice"},
            {"_id": "b", "displayName": "Bob"},
        ],
        "selections": list(rows),
        "blockedProfileIds": [],
        **kwargs,
    }


class WhenTest(TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.bot = FakeBot(self.db)
        when.init(self.bot)

    def query(
        self,
        data: Any,
        now: str = "2026-09-04T10:15:00+00:00",
        lookahead_hours: float = 0,
    ) -> whenapi.Availability | None:
        response = Response(
            "https://when-convex.nyanya.org/api/query",
            200,
            "OK",
            {},
            json.dumps(data).encode(),
        )
        with patch.object(whenapi.http, "request", return_value=response):
            return whenapi.get_availability(
                self.bot,
                LINK,
                now=datetime.fromisoformat(now),
                lookahead_hours=lookahead_hours,
            )

    def available(
        self, data: dict[str, Any], now: str = "2026-09-04T10:15:00+00:00"
    ) -> tuple[str, ...]:
        result = self.query({"status": "success", "value": data}, now)
        assert result is not None
        return tuple(window.name for window in result.available)

    def test_link_validation_and_canonicalization(self) -> None:
        self.assertEqual(whenapi.parse_link(self.bot, URL + "/?view=week#grid"), LINK)
        for url in (
            "https://evil.example/schedule/" + SCHEDULE_ID,
            URL.replace("when.nyanya.org", "when.nyanya.org.evil.example"),
            URL.replace("when.nyanya.org", "user@when.nyanya.org"),
            URL.replace("https:", "http:"),
            URL + "/edit",
            URL + "\n",
            URL[:-1],
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                whenapi.parse_link(self.bot, url)

    def test_public_query_contract(self) -> None:
        response = Response(
            "https://when-convex.nyanya.org/api/query",
            200,
            "OK",
            {},
            b'{"status":"success","value":null}',
        )
        with patch.object(whenapi.http, "request", return_value=response) as request:
            self.assertIsNone(whenapi.get_availability(self.bot, LINK))
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", "https://when-convex.nyanya.org/api/query"))
        self.assertEqual(
            json.loads(kwargs["body"]),
            {
                "path": "schedules:get",
                "args": {"scheduleId": SCHEDULE_ID},
                "format": "json",
            },
        )

    def test_half_hour_boundaries(self) -> None:
        data = schedule(selection())
        for moment, expected in (
            ("09:59:59", ()),
            ("10:00:00", ("Alice",)),
            ("10:29:59", ("Alice",)),
            ("10:30:00", ()),
        ):
            with self.subTest(moment=moment):
                self.assertEqual(
                    self.available(data, "2026-09-04T" + moment + "+00:00"), expected
                )

    def test_only_confirmed_nonblocked_participants_are_available(self) -> None:
        self.assertEqual(
            self.available(schedule(selection(), selection("b", state="maybe"))),
            ("Alice",),
        )
        self.assertEqual(
            self.available(schedule(selection(), blockedProfileIds=["a"])), ()
        )
        self.assertEqual(
            self.available(schedule(selection(), selection(state="cant-do"))), ()
        )
        self.assertEqual(self.available(schedule(selection(), selection())), ("Alice",))

    def test_local_weekday_and_date_across_midnight(self) -> None:
        # Friday UTC is already Saturday in Melbourne.
        row = selection(dayKey="6", timeSlot="00:00", timezone="Australia/Melbourne")
        self.assertEqual(
            self.available(schedule(row), "2026-09-04T14:15:00+00:00"), ("Alice",)
        )
        row = selection(
            dayKey="2026-09-05", timeSlot="00:00", timezone="Australia/Melbourne"
        )
        self.assertEqual(
            self.available(schedule(row, type="one-off"), "2026-09-04T14:15:00+00:00"),
            ("Alice",),
        )

    def test_quarter_hour_timezone_and_slot_crossing_midnight(self) -> None:
        row = selection(timeSlot="16:00", timezone="Asia/Kathmandu")
        self.assertEqual(self.available(schedule(row)), ("Alice",))
        row = selection(dayKey="4", timeSlot="23:45")
        self.assertEqual(
            self.available(schedule(row), "2026-09-04T00:10:00+00:00"), ("Alice",)
        )
        self.assertEqual(self.available(schedule(row), "2026-09-04T00:15:00+00:00"), ())

    def test_dst_changes_follow_participant_wall_clock(self) -> None:
        row = selection(dayKey="0", timeSlot="10:00", timezone="America/New_York")
        for moment in ("2026-01-04T15:15:00+00:00", "2026-07-05T14:15:00+00:00"):
            self.assertEqual(self.available(schedule(row), moment), ("Alice",))
        row = selection(dayKey="0", timeSlot="02:00", timezone="America/New_York")
        self.assertEqual(self.available(schedule(row), "2026-03-08T07:15:00+00:00"), ())
        row = selection(dayKey="0", timeSlot="01:00", timezone="America/New_York")
        for moment in ("2026-11-01T05:15:00+00:00", "2026-11-01T06:15:00+00:00"):
            self.assertEqual(self.available(schedule(row), moment), ("Alice",))

    def test_exception_overrides_only_its_profile_on_its_date(self) -> None:
        override = selection(
            isException=True,
            exceptionDate="2026-09-04",
            state="cant-do",
            source="calendar",
        )
        data = schedule(selection(), selection("b"), override)
        self.assertEqual(self.available(data), ("Bob",))
        self.assertEqual(
            self.available(data, "2026-09-11T10:15:00+00:00"), ("Alice", "Bob")
        )
        override["state"] = "can-do"
        self.assertEqual(
            self.available(schedule(selection(state="cant-do"), override)), ("Alice",)
        )

    def test_exception_uses_its_own_timezone(self) -> None:
        override = selection(
            dayKey="5",
            timeSlot="20:00",
            timezone="Australia/Melbourne",
            isException=True,
            exceptionDate="2026-09-04",
            state="maybe",
        )
        self.assertEqual(self.available(schedule(selection(), override)), ())

    def test_virtual_selections_from_linked_availability_are_used(self) -> None:
        row = selection(_id="virtual_link_5_10:00")
        self.assertEqual(
            self.available(schedule(row, availabilityLinks=[{"profileId": "a"}])),
            ("Alice",),
        )

    def test_date_ranges_and_disallowed_slots_use_creator_timezone(self) -> None:
        self.assertEqual(
            self.available(schedule(selection(), recurringStartDate="2026-09-05")), ()
        )
        row = selection(dayKey="2026-09-04")
        self.assertEqual(
            self.available(schedule(row, type="one-off", dateRangeEnd="2026-09-03")), ()
        )
        data = schedule(
            selection(),
            creatorTimezone="Australia/Melbourne",
            disallowedSlots=[{"dayKey": "5", "timeSlot": "20:00"}],
        )
        self.assertEqual(self.available(data), ())
        self.assertEqual(
            self.available(schedule(selection(), isLocked=True, lockedSlots=[])),
            ("Alice",),
        )

    def test_malformed_responses_are_service_errors(self) -> None:
        data: Any
        for data in (
            [],
            {"status": "error"},
            {"status": "success"},
            {"status": "success", "value": []},
            {
                "status": "success",
                "value": schedule(selection(timezone="Bad/Timezone")),
            },
            {"status": "success", "value": schedule(selection(timeSlot="99:00"))},
            {"status": "success", "value": schedule(selection(state="yes"))},
            {"status": "success", "value": schedule(selection(), profiles=None)},
        ):
            with self.subTest(data=data), self.assertRaises(whenapi.WhenAPIError):
                self.query(data)

    def windows(
        self,
        data: dict[str, Any],
        *,
        lookahead: float = 1.7,
        now: str = "2026-09-04T10:15:00+00:00",
    ) -> tuple[whenapi.AvailabilityWindow, ...]:
        result = self.query({"status": "success", "value": data}, now, lookahead)
        assert result is not None
        return result.available

    def test_current_duration_merges_adjacent_slots_and_uses_remaining_time(
        self,
    ) -> None:
        data = schedule(
            *(
                selection(timeSlot=slot)
                for slot in ("10:00", "10:30", "11:00", "11:30", "12:00")
            )
        )
        self.assertEqual(
            self.windows(data, lookahead=0),
            (whenapi.AvailabilityWindow("Alice", 0, 2.25),),
        )

    def test_future_window_uses_full_duration_beyond_lookahead(self) -> None:
        data = schedule(
            selection(),
            *(
                selection("b", timeSlot=slot)
                for slot in ("11:30", "12:00", "12:30", "13:00")
            ),
        )
        self.assertEqual(
            self.windows(data),
            (
                whenapi.AvailabilityWindow("Alice", 0, 0.25),
                whenapi.AvailabilityWindow("Bob", 1.25, 2),
            ),
        )
        self.assertEqual(
            self.windows(data, lookahead=1),
            (whenapi.AvailabilityWindow("Alice", 0, 0.25),),
        )

    def test_lookahead_is_inclusive_and_preserves_fractional_hours(self) -> None:
        data = schedule(selection(timeSlot="11:57"))
        self.assertEqual(
            self.windows(data), (whenapi.AvailabilityWindow("Alice", 1.7, 0.5),)
        )
        self.assertEqual(self.windows(data, lookahead=1.69), ())
        self.assertEqual(self.windows(data, lookahead=0), ())

    def test_only_first_run_per_person_and_gaps_are_not_merged(self) -> None:
        data = schedule(selection(), selection(timeSlot="11:00"))
        self.assertEqual(
            self.windows(data), (whenapi.AvailabilityWindow("Alice", 0, 0.25),)
        )
        self.assertEqual(
            self.windows(data, now="2026-09-04T10:30:00+00:00"),
            (whenapi.AvailabilityWindow("Alice", 0.5, 0.5),),
        )

    def test_exceptions_and_disallowed_slots_split_duration(self) -> None:
        rows = [
            selection(timeSlot=slot) for slot in ("10:00", "10:30", "11:00", "11:30")
        ]
        override = selection(
            timeSlot="10:30",
            isException=True,
            exceptionDate="2026-09-04",
            state="maybe",
        )
        self.assertEqual(
            self.windows(schedule(*rows, override)),
            (whenapi.AvailabilityWindow("Alice", 0, 0.25),),
        )
        self.assertEqual(
            self.windows(schedule(*rows, override), now="2026-09-04T10:30:00+00:00"),
            (whenapi.AvailabilityWindow("Alice", 0.5, 1),),
        )
        self.assertEqual(
            self.windows(
                schedule(*rows, disallowedSlots=[{"dayKey": "5", "timeSlot": "11:00"}])
            ),
            (whenapi.AvailabilityWindow("Alice", 0, 0.75),),
        )

    def test_midnight_and_schedule_end_bound_duration(self) -> None:
        data = schedule(
            selection(dayKey="5", timeSlot="23:30"),
            selection(dayKey="6", timeSlot="00:00"),
        )
        self.assertEqual(
            self.windows(data, now="2026-09-04T23:45:00+00:00"),
            (whenapi.AvailabilityWindow("Alice", 0, 0.75),),
        )
        data = schedule(
            selection(dayKey="2026-09-04", timeSlot="23:45"),
            type="one-off",
            dateRangeEnd="2026-09-04",
        )
        self.assertEqual(
            self.windows(data, now="2026-09-04T23:30:00+00:00"),
            (whenapi.AvailabilityWindow("Alice", 0.25, 0.25),),
        )

    def test_duration_across_dst_is_elapsed_time(self) -> None:
        data = schedule(
            *(
                selection(dayKey="0", timeSlot=slot, timezone="America/New_York")
                for slot in ("01:00", "01:30", "02:00")
            )
        )
        self.assertEqual(
            self.windows(data, now="2026-11-01T05:15:00+00:00"),
            (whenapi.AvailabilityWindow("Alice", 0, 2.25),),
        )
        data = schedule(
            *(
                selection(dayKey="0", timeSlot=slot, timezone="America/New_York")
                for slot in ("01:30", "03:00")
            )
        )
        self.assertEqual(
            self.windows(data, now="2026-03-08T06:45:00+00:00"),
            (whenapi.AvailabilityWindow("Alice", 0, 0.75),),
        )

    def test_continuous_recurring_availability_is_an_explicit_lower_bound(self) -> None:
        rows = [
            selection(dayKey=str(day), timeSlot=f"{hour:02d}:{minute:02d}")
            for day in range(7)
            for hour in range(24)
            for minute in (0, 30)
        ]
        self.assertEqual(
            self.windows(schedule(*rows), lookahead=0),
            (whenapi.AvailabilityWindow("Alice", 0, 192, True),),
        )

    def test_invalid_lookahead_is_rejected_before_request(self) -> None:
        with patch.object(whenapi.http, "request") as request:
            for lookahead in (-1, float("nan"), float("inf"), 169):
                with self.subTest(lookahead=lookahead), self.assertRaises(ValueError):
                    whenapi.get_availability(self.bot, LINK, lookahead_hours=lookahead)
            request.assert_not_called()

    def command(self, argument: str | None = None, channel: str = "#Room[") -> None:
        when.when(Event("privmsged", target=channel, argument=argument), self.bot)

    def test_links_persist_and_are_scoped_by_network_and_irc_channel(self) -> None:
        with patch.object(
            whenapi,
            "get_availability",
            return_value=whenapi.Availability(
                (whenapi.AvailabilityWindow("Alice", 0, 0.5),)
            ),
        ) as query:
            self.command("~link " + URL)
            # Reinitializing a module must preserve the saved link.
            when.init(self.bot)
            self.command(channel="#room{")
            self.assertEqual(query.call_count, 2)
            self.assertEqual(query.call_args.kwargs["lookahead_hours"], 1.7)
            self.command(channel="#other")
            self.bot.network = "other-network"
            self.command()
            self.assertEqual(query.call_count, 2)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM when_links").fetchone()[0], 1
        )

    def test_failed_relink_preserves_saved_schedule(self) -> None:
        with patch.object(
            whenapi, "get_availability", return_value=whenapi.Availability(())
        ):
            self.command("~link " + URL)
        other_url = URL.replace(SCHEDULE_ID, "a" * 32)
        for result in (None, HTTPError("offline")):
            with patch.object(
                whenapi,
                "get_availability",
                side_effect=result if isinstance(result, Exception) else None,
                return_value=None,
            ):
                if isinstance(result, Exception):
                    with self.assertLogs(when.log, level="ERROR"):
                        self.command("~link " + other_url)
                else:
                    self.command("~link " + other_url)
            self.assertEqual(
                self.db.execute("SELECT url FROM when_links").fetchone()[0], URL
            )

    def test_pm_and_unlinked_queries_do_not_fetch_or_write(self) -> None:
        with patch.object(whenapi, "get_availability") as query:
            self.command()
            self.command("~link " + URL, channel="BotNick")
            self.command("~link https://evil.example/")
            query.assert_not_called()
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM when_links").fetchone()[0], 0
        )

    def test_unlink_only_removes_current_channels_link(self) -> None:
        with patch.object(
            whenapi, "get_availability", return_value=whenapi.Availability(())
        ):
            self.command("~link " + URL)
            self.command("~link " + URL, channel="#other")
        self.command("~unlink")
        self.assertEqual(
            [row[0] for row in self.db.execute("SELECT channel FROM when_links")],
            ["#other"],
        )
