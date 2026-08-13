from dataclasses import dataclass
from urllib.parse import quote, urlencode

from util.http import HTTPError, HTTPStatusError, http
from util.types import BotLike


DICTIONARY_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/%s"
DATAMUSE_URL = "https://api.datamuse.com/words?%s"
DATAMUSE_ATTRIBUTION = "Datamuse: https://www.datamuse.com/api/"
MAX_PARTS_OF_SPEECH = 4
MAX_DEFINITIONS_PER_PART = 3
MAX_SYNONYMS = 20
MAX_SPELLING_SUGGESTIONS = 10


class WordsAPIError(HTTPError):
    """The remote service returned a response that violates its documented schema."""


@dataclass(frozen=True, slots=True)
class DefinitionGroup:
    part_of_speech: str
    definitions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DictionaryResult:
    groups: tuple[DefinitionGroup, ...]
    synonyms: tuple[str, ...]
    source_url: str
    license_name: str
    license_url: str

    @property
    def attribution(self) -> str:
        source = self.source_url or "https://dictionaryapi.dev/"
        if self.license_name:
            return "%s [%s]" % (source, self.license_name)
        return source


@dataclass(frozen=True, slots=True)
class WordListResult:
    words: tuple[str, ...]
    attribution: str | None = None


def _normalized_text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _unique_strings(values: object, *, limit: int | None = None) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalized_text(value)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        output.append(normalized)
        if limit is not None and len(output) == limit:
            break
    return tuple(output)


def _dictionary_lookup(query: str) -> DictionaryResult | None:
    try:
        payload = http.get_json(DICTIONARY_URL % quote(query, safe=""))
    except HTTPStatusError as exc:
        if exc.response.status == 404:
            return None
        raise
    if not isinstance(payload, list):
        raise WordsAPIError("Free Dictionary API returned a non-list response.")

    grouped_definitions: dict[str, list[str]] = {}
    synonyms: list[str] = []
    source_url = ""
    license_name = ""
    license_url = ""
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if not source_url:
            source_urls = _unique_strings(entry.get("sourceUrls"), limit=1)
            source_url = source_urls[0] if source_urls else ""
        license_data = entry.get("license")
        if not license_name and isinstance(license_data, dict):
            license_name = _normalized_text(license_data.get("name"))
            license_url = _normalized_text(license_data.get("url"))

        meanings = entry.get("meanings")
        if not isinstance(meanings, list):
            continue
        for meaning in meanings:
            if not isinstance(meaning, dict):
                continue
            part_of_speech = _normalized_text(meaning.get("partOfSpeech")) or "?"
            definitions = grouped_definitions.setdefault(part_of_speech, [])
            synonyms.extend(_unique_strings(meaning.get("synonyms")))
            definition_items = meaning.get("definitions")
            if not isinstance(definition_items, list):
                continue
            for definition_item in definition_items:
                if not isinstance(definition_item, dict):
                    continue
                synonyms.extend(_unique_strings(definition_item.get("synonyms")))
                definition = _normalized_text(definition_item.get("definition"))
                if definition and definition.casefold() not in {
                    item.casefold() for item in definitions
                }:
                    definitions.append(definition)

    groups = tuple(
        DefinitionGroup(part, tuple(definitions[:MAX_DEFINITIONS_PER_PART]))
        for part, definitions in grouped_definitions.items()
        if definitions
    )[:MAX_PARTS_OF_SPEECH]
    if not groups:
        return None
    return DictionaryResult(
        groups=groups,
        synonyms=_unique_strings(synonyms, limit=MAX_SYNONYMS),
        source_url=source_url,
        license_name=license_name,
        license_url=license_url,
    )


def _datamuse_words(parameters: dict[str, str | int]) -> tuple[str, ...]:
    payload = http.get_json(DATAMUSE_URL % urlencode(parameters))
    if not isinstance(payload, list):
        raise WordsAPIError("Datamuse returned a non-list response.")
    words: list[str] = []
    for item in payload:
        if isinstance(item, dict):
            word = _normalized_text(item.get("word"))
            if word:
                words.append(word)
    return _unique_strings(words)


def spell_check(
    bot: BotLike, query: str, skip_search: bool = False
) -> tuple[str, ...] | None:
    """Return ranked corrections, or None when the spelling appears valid."""
    del bot  # The compatibility API accepts a bot context but needs no settings.
    normalized_query = _normalized_text(query)
    if not normalized_query:
        return ()
    if not skip_search and _dictionary_lookup(normalized_query) is not None:
        return None
    candidates = _datamuse_words(
        {"sp": normalized_query, "max": MAX_SPELLING_SUGGESTIONS}
    )
    query_key = normalized_query.casefold()
    if any(candidate.casefold() == query_key for candidate in candidates):
        return None
    return tuple(
        candidate for candidate in candidates if candidate.casefold() != query_key
    )


def word_search(bot: BotLike, query: str) -> DictionaryResult | None:
    """Return a bounded, attributed English dictionary result."""
    del bot
    normalized_query = _normalized_text(query)
    return _dictionary_lookup(normalized_query) if normalized_query else None


def word_synonyms(bot: BotLike, query: str) -> WordListResult | None:
    """Return synonyms from the dictionary, falling back to Datamuse/WordNet."""
    del bot
    normalized_query = _normalized_text(query)
    if not normalized_query:
        return None
    dictionary_result = _dictionary_lookup(normalized_query)
    if dictionary_result and dictionary_result.synonyms:
        return WordListResult(dictionary_result.synonyms, dictionary_result.attribution)

    synonyms = _datamuse_words({"rel_syn": normalized_query, "max": MAX_SYNONYMS})
    if synonyms:
        return WordListResult(synonyms, DATAMUSE_ATTRIBUTION)
    if dictionary_result:
        return WordListResult(())
    return None


def init(bot: BotLike) -> bool:
    return True
