from unittest import TestCase

from pyburlybot_modules.time import _timezone_reply


class TimezoneReplyTest(TestCase):
    def test_utc_offsets(self) -> None:
        reply = _timezone_reply("utc-11")
        assert reply is not None
        self.assertTrue(reply.endswith("UTC-11"))
        reply = _timezone_reply("UTC+5:30")
        assert reply is not None
        self.assertTrue(reply.endswith("UTC+5:30"))
        reply = _timezone_reply("gmt")
        assert reply is not None
        self.assertTrue(reply.endswith("UTC"))

    def test_iana_zone_names_are_case_insensitive(self) -> None:
        reply = _timezone_reply("america/chicago")
        assert reply is not None
        self.assertIn("America/Chicago", reply)
        # tzdata's legacy fixed-offset abbreviation zones work too
        self.assertIsNotNone(_timezone_reply("est"))

    def test_non_timezone_queries_fall_through(self) -> None:
        self.assertIsNone(_timezone_reply("Coatarricense University"))
        self.assertIsNone(_timezone_reply("utc+15"))
        self.assertIsNone(_timezone_reply("12345"))
