from types import SimpleNamespace
from typing import Any, cast
from unittest import TestCase

from util.state import Network, User


class StateTest(TestCase):
    def make_network(self) -> Network:
        network = Network("test")
        network.prefixmap = cast(
            Any,
            SimpleNamespace(
                opcmds={"o"},
                voicecmds={"v"},
                nickprefixes="@+",
                opprefixes="@",
                voiceprefixes="+",
            ),
        )
        network._joinchannel("#one")
        return network

    def test_user_keeps_complete_hostmask(self) -> None:
        user = User("Nick", "ident", "host", "Nick!ident@host")
        self.assertEqual(user.hostmask, "Nick!ident@host")

    def test_part_removes_channel_before_pruning_user(self) -> None:
        network = self.make_network()
        network._userjoin("#one", "Nick", "ident", "host", "Nick!ident@host")
        network._userpart("#one", "Nick")
        self.assertNotIn("Nick", network.users)
        self.assertNotIn("Nick", network.channels["#one"].users)

    def test_incremental_mode_does_not_clear_existing_modes(self) -> None:
        network = self.make_network()
        network._userjoin("#one", "Nick")
        network._modechange("#one", None, [("m", "")], [], reset=True)
        network._modechange("#one", "Operator", [("v", "Nick")], [])
        channel = network.channels["#one"]
        self.assertTrue(channel.moderated)
        self.assertEqual(channel.voices, {"Nick"})

    def test_topic_and_snapshot_are_detached(self) -> None:
        network = self.make_network()
        network._settopic("#one", "Topic", "Nick", "ident", "host", "Nick!ident@host")
        snapshot = network.snapshot()
        network.channels["#one"].topic = "Changed"
        self.assertEqual(snapshot["channels"]["#one"]["topic"], "Topic")
        self.assertEqual(
            network.channels["#one"].topicsetby,
            ("Nick", "ident", "host", "Nick!ident@host"),
        )
