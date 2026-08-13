from unittest import TestCase
from warnings import catch_warnings, simplefilter

from util.helpers import argumentSplit, match_hostmask, parseDateTime


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

    def test_hostmask_glob_is_anchored_and_uses_rfc1459_casefold(self) -> None:
        self.assertTrue(match_hostmask("Nick!ident@example.com", "n?ck!*@*.com"))
        self.assertTrue(match_hostmask("[Nick]!u@host", "{nick}!*@host"))
        self.assertTrue(match_hostmask("nick!u@host", "*!*@*"))
        self.assertFalse(match_hostmask("prefixnick!u@host.evil", "nick!*@host"))
        self.assertFalse(match_hostmask("nick!u@host", "nick!*@*.example"))
