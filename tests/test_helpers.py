from calendar import timegm
from datetime import datetime
from unittest import TestCase
from warnings import catch_warnings, simplefilter

from util.helpers import argumentSplit, match_hostmask, parseDateTime


def _epoch(year: int, month: int, day: int, hour: int, minute: int) -> int:
    walltime = datetime(year, month, day, hour, minute)  # noqa: DTZ001 - UTC walltime
    return timegm(walltime.timetuple())


class HelpersTest(TestCase):
    def test_splits_quoted_and_unicode_arguments(self) -> None:
        self.assertEqual(
            argumentSplit('café "two words" 😀', -1),
            ["café", "two words", "😀"],
        )

    def test_preserves_the_unsplit_final_argument(self) -> None:
        self.assertEqual(
            argumentSplit('one "two three" four', 2),
            ["one", '"two three" four'],
        )

    def test_treats_punctuation_as_word_characters(self) -> None:
        self.assertEqual(
            argumentSplit(r"#channel one\two https://example.com/a?x=1&y=2", -1),
            ["#channel", r"one\two", "https://example.com/a?x=1&y=2"],
        )

    def test_date_parsing_uses_the_supported_utc_constructor(self) -> None:
        with catch_warnings():
            simplefilter("error", DeprecationWarning)
            self.assertEqual(parseDateTime("tomorrow", 1_700_000_000), 1_700_031_600)

    def test_date_parsing_survives_month_ends(self) -> None:
        # every case here used to raise ValueError (or land in month 13)
        self.assertEqual(
            parseDateTime("tomorrow", _epoch(2026, 1, 31, 18, 0)),
            _epoch(2026, 2, 1, 7, 0),
        )
        self.assertEqual(
            parseDateTime("at 5:00", _epoch(2026, 12, 31, 18, 0)),
            _epoch(2027, 1, 1, 5, 0),
        )
        self.assertEqual(
            parseDateTime("at lunch", _epoch(2026, 1, 31, 13, 0)),
            _epoch(2026, 2, 1, 12, 0),
        )
        # Tuesday June 30th: next Monday crosses into July
        self.assertEqual(
            parseDateTime("on monday", _epoch(2026, 6, 30, 10, 0)),
            _epoch(2026, 7, 6, 0, 0),
        )
        # day already passed and February cannot hold a 31st
        self.assertEqual(
            parseDateTime("on 31st", _epoch(2026, 1, 31, 18, 0)),
            _epoch(2026, 3, 31, 0, 0),
        )
        # day already passed in December: wrap into next year
        self.assertEqual(
            parseDateTime("on 20th", _epoch(2026, 12, 25, 9, 0)),
            _epoch(2027, 1, 20, 0, 0),
        )

    def test_hostmask_glob_is_anchored_and_uses_rfc1459_casefold(self) -> None:
        self.assertTrue(match_hostmask("Nick!ident@example.com", "n?ck!*@*.com"))
        self.assertTrue(match_hostmask("[Nick]!u@host", "{nick}!*@host"))
        self.assertTrue(match_hostmask("nick!u@host", "*!*@*"))
        self.assertFalse(match_hostmask("prefixnick!u@host.evil", "nick!*@host"))
        self.assertFalse(match_hostmask("nick!u@host", "nick!*@*.example"))
