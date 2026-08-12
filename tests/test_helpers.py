from unittest import TestCase
from warnings import catch_warnings, simplefilter

from util.helpers import argumentSplit, parseDateTime


class HelpersTest(TestCase):
	def test_splits_quoted_and_unicode_arguments(self):
		self.assertEqual(
			argumentSplit('café "two words" 😀', -1),
			["café", "two words", "😀"],
		)

	def test_preserves_the_unsplit_final_argument(self):
		self.assertEqual(
			argumentSplit('one "two three" four', 2),
			["one", '"two three" four'],
		)

	def test_treats_punctuation_as_word_characters(self):
		self.assertEqual(
			argumentSplit(r'#channel one\two https://example.com/a?x=1&y=2', -1),
			["#channel", r"one\two", "https://example.com/a?x=1&y=2"],
		)

	def test_date_parsing_uses_the_supported_utc_constructor(self):
		with catch_warnings():
			simplefilter("error", DeprecationWarning)
			self.assertEqual(parseDateTime("tomorrow", 1_700_000_000), 1_700_031_600)
