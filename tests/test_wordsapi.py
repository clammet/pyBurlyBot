from typing import Any, cast
from unittest import TestCase
from unittest.mock import patch

from pyburlybot_modules import words, wordsapi
from util.event import Event
from util.http import HTTPStatusError, Response
from util.types import BotLike


BOT = cast(BotLike, object())


def dictionary_payload(*, synonyms: list[str] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "word": "example",
            "meanings": [
                {
                    "partOfSpeech": "noun",
                    "synonyms": synonyms or [],
                    "definitions": [
                        {
                            "definition": " Something representative of a group. ",
                            "synonyms": ["instance", "instance"],
                        },
                        {"definition": "Something that illustrates a rule."},
                    ],
                },
                {
                    "partOfSpeech": "verb",
                    "definitions": [{"definition": "To illustrate."}],
                },
            ],
            "license": {
                "name": "CC BY-SA 3.0",
                "url": "https://creativecommons.org/licenses/by-sa/3.0",
            },
            "sourceUrls": ["https://en.wiktionary.org/wiki/example"],
        }
    ]


class FakeWordsBot:
    network = "words-test"

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def say(self, msg: Any, **kwargs: Any) -> None:
        self.messages.append(str(msg))

    def getOption(self, opt: str, **kwargs: Any) -> Any:
        raise AttributeError(opt)

    def setOption(self, opt: str, value: Any, **kwargs: Any) -> None:
        raise AttributeError(opt)

    def getModule(self, name: str) -> Any:
        if name == "wordsapi":
            return wordsapi
        raise AttributeError(name)


class WordsAPITest(TestCase):
    def test_parses_bounded_attributed_definitions_and_synonyms(self) -> None:
        with patch.object(wordsapi.http, "get_json", return_value=dictionary_payload()):
            result = wordsapi.word_search(BOT, " example ")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            result.groups[0].definitions,
            (
                "Something representative of a group.",
                "Something that illustrates a rule.",
            ),
        )
        self.assertEqual(result.synonyms, ("instance",))
        self.assertEqual(result.license_name, "CC BY-SA 3.0")
        self.assertEqual(result.source_url, "https://en.wiktionary.org/wiki/example")

    def test_dictionary_404_is_a_normal_miss(self) -> None:
        error = HTTPStatusError(
            Response(
                url="https://api.dictionaryapi.dev/api/v2/entries/en/missing",
                status=404,
                reason="Not Found",
                headers={},
                body=b"",
            )
        )
        with patch.object(wordsapi.http, "get_json", side_effect=error):
            self.assertIsNone(wordsapi.word_search(BOT, "missing"))

    def test_spelling_uses_ranked_datamuse_results(self) -> None:
        with patch.object(
            wordsapi.http,
            "get_json",
            return_value=[{"word": "example"}, {"word": "examine"}],
        ):
            suggestions = wordsapi.spell_check(BOT, "exampel", skip_search=True)
        self.assertEqual(suggestions, ("example", "examine"))

    def test_exact_datamuse_match_counts_as_valid_spelling(self) -> None:
        with patch.object(wordsapi.http, "get_json", return_value=[{"word": "colour"}]):
            self.assertIsNone(wordsapi.spell_check(BOT, "Colour", skip_search=True))

    def test_synonyms_prefer_dictionary_data(self) -> None:
        with patch.object(
            wordsapi.http,
            "get_json",
            return_value=dictionary_payload(synonyms=["model"]),
        ) as request:
            result = wordsapi.word_synonyms(BOT, "example")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.words, ("model", "instance"))
        self.assertIn("https://en.wiktionary.org/", result.attribution or "")
        request.assert_called_once()

    def test_synonyms_fall_back_to_datamuse(self) -> None:
        payload = dictionary_payload()
        payload[0]["meanings"][0]["definitions"][0]["synonyms"] = []
        with patch.object(
            wordsapi.http,
            "get_json",
            side_effect=[payload, [{"word": "sample"}, {"word": "illustration"}]],
        ):
            result = wordsapi.word_synonyms(BOT, "example")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.words, ("sample", "illustration"))
        self.assertEqual(result.attribution, wordsapi.DATAMUSE_ATTRIBUTION)

    def test_dictionary_reply_puts_attribution_before_trimmable_content(self) -> None:
        bot = FakeWordsBot()
        event = Event("privmsged", argument="example")
        with patch.object(wordsapi.http, "get_json", return_value=dictionary_payload()):
            words.dictionary(event, bot)
        self.assertTrue(
            bot.messages[0].startswith("Source: https://en.wiktionary.org/")
        )
