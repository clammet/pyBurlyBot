from calendar import timegm
from datetime import UTC, datetime
from time import gmtime
from typing import Any, cast
from unittest import TestCase
from zoneinfo import ZoneInfo

from util.types import BotLike

from pyburlybot_modules.remind_common import (
    _walltime_to_epoch,
    parse_remind_args,
    resolve_user_time,
)


class ParseRemindArgsTest(TestCase):
    def test_packed_datespec_still_parses(self) -> None:
        self.assertEqual(
            parse_remind_args("me in 3days do stuff"),
            ("ok", "me", "in 3days", "do stuff"),
        )

    def test_spaced_datespec_pulls_unit_from_msg(self) -> None:
        self.assertEqual(
            parse_remind_args("me in 3 days do stuff"),
            ("ok", "me", "in 3 days", "do stuff"),
        )

    def test_spaced_compound_datespec(self) -> None:
        self.assertEqual(
            parse_remind_args("me in 1h 30m do stuff"),
            ("ok", "me", "in 1h 30m", "do stuff"),
        )

    def test_pull_does_not_swallow_message_words(self) -> None:
        self.assertEqual(
            parse_remind_args("me in 3 days days are numbered"),
            ("ok", "me", "in 3 days", "days are numbered"),
        )

    def test_datespec_consuming_whole_argument_reports_missing_msg(self) -> None:
        self.assertEqual(parse_remind_args("me in 3 days")[0], "msg")

    def test_tomorrow_special_case(self) -> None:
        self.assertEqual(
            parse_remind_args("me tomorrow do stuff"),
            ("ok", "me", "tomorrow", "do stuff"),
        )


class _FakeLocationModule:
    # mirrors googleapi.google_timezone's (timeZoneId, name, dstOffset,
    # rawOffset) shape, with offsets valid at `when` like the real API
    @staticmethod
    def get_user_timezone(
        bot: Any, user: str, when: int | float
    ) -> tuple[str, str, int, int]:
        local = datetime.fromtimestamp(when, ZoneInfo("America/New_York"))
        offset = local.utcoffset()
        dst = local.dst()
        assert offset is not None and dst is not None
        dst_seconds = int(dst.total_seconds())
        raw_seconds = int(offset.total_seconds()) - dst_seconds
        return ("America/New_York", "Eastern Time", dst_seconds, raw_seconds)


class _FakeBot:
    @staticmethod
    def getModule(name: str) -> Any:
        assert name == "location"
        return _FakeLocationModule


class ResolveUserTimeTest(TestCase):
    def test_walltime_converts_with_target_instant_offset(self) -> None:
        zone = ZoneInfo("America/New_York")
        # 2026-11-02 09:00 walltime is after the 2026-11-01 fall-back: EST (-5)
        walltime = timegm(datetime(2026, 11, 2, 9, 0, tzinfo=UTC).timetuple())
        self.assertEqual(
            datetime.fromtimestamp(_walltime_to_epoch(walltime, zone), UTC),
            datetime(2026, 11, 2, 14, 0, tzinfo=UTC),
        )
        # 2026-07-01 09:00 walltime is in EDT (-4)
        walltime = timegm(datetime(2026, 7, 1, 9, 0, tzinfo=UTC).timetuple())
        self.assertEqual(
            datetime.fromtimestamp(_walltime_to_epoch(walltime, zone), UTC),
            datetime(2026, 7, 1, 13, 0, tzinfo=UTC),
        )

    def test_relative_specs_stay_pure_durations(self) -> None:
        ntime, _current_time, origintime = resolve_user_time(
            cast(BotLike, _FakeBot()), "user", "in 3 hours"
        )
        assert ntime is not None
        self.assertAlmostEqual(ntime - origintime, 3 * 3600, delta=2)

    def test_absolute_specs_land_on_the_users_walltime(self) -> None:
        ntime, _current_time, _origintime = resolve_user_time(
            cast(BotLike, _FakeBot()), "user", "at 5:00"
        )
        assert ntime is not None
        local = datetime.fromtimestamp(ntime, ZoneInfo("America/New_York"))
        self.assertEqual((local.hour, local.minute), (5, 0))
        self.assertGreater(ntime, timegm(gmtime()) - 2)
