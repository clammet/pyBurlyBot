from itertools import islice
from unittest import TestCase

from pyburlybot_modules.charinfo import MAX_CODEPOINT, REGHEX, _search_names


class CharacterInfoTest(TestCase):
    def test_accepts_supplementary_plane_code_points(self) -> None:
        match = REGHEX.fullmatch("U+1F602")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertLessEqual(int(match.group(1), 16), MAX_CODEPOINT)

    def test_name_search_yields_exact_match_without_building_an_index(self) -> None:
        results = list(islice(_search_names("FACE WITH TEARS OF JOY"), 1))
        self.assertEqual(results, [(0x1F602, "FACE WITH TEARS OF JOY")])
