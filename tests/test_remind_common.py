from unittest import TestCase

from pyburlybot_modules.remind_common import parse_remind_args


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
