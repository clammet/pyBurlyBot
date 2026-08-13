from unittest import TestCase

from pyburlybot_modules.users import _user_update
from util.event import Event


class SeenPrivacyTest(TestCase):
    def stored_message(self, event: Event) -> str | None:
        captured = []
        _user_update(lambda query, params: captured.append(params), event)
        return captured[0][-1]

    def test_private_message_content_is_redacted(self) -> None:
        event = Event("privmsged", target="BurlyBot", nick="Nick", msg="secret")
        self.assertEqual(self.stored_message(event), "Private message")

    def test_admin_command_content_is_redacted(self) -> None:
        event = Event(
            "privmsged",
            target="#channel",
            nick="Nick",
            msg="!config this - password secret",
            is_admin_command=True,
        )
        self.assertEqual(self.stored_message(event), "Command")

    def test_public_non_admin_content_is_retained(self) -> None:
        event = Event("privmsged", target="#channel", nick="Nick", msg="hello")
        self.assertEqual(self.stored_message(event), "hello")
