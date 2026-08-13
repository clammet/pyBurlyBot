import sqlite3
from unittest import TestCase

from pyburlybot_modules.alias import group_list


class AliasGroupTest(TestCase):
    def test_group_list_accepts_direct_names_and_group_aliases(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE aliasgrp (grp TEXT, user TEXT)")
        connection.execute("CREATE TABLE aliasgrpalias (alias TEXT, grp TEXT)")
        connection.execute("INSERT INTO aliasgrp VALUES ('friends', 'alice')")
        connection.execute("INSERT INTO aliasgrpalias VALUES ('mates', 'friends')")

        def query(
            statement: str, params: tuple = (), function: object = None
        ) -> object:
            cursor = connection.execute(statement, params)
            return function(cursor) if callable(function) else cursor.fetchall()

        self.assertEqual(
            [row for row in group_list(query, "friends")],
            ["alice"],
        )
        self.assertEqual(group_list(query, "mates"), ["alice"])
