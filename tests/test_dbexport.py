from io import StringIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from dbexport import export_database, import_database, main


class DBExportTest(TestCase):
    def _make_source(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        with connection:
            connection.execute(
                """CREATE TABLE tell(
                    id INTEGER PRIMARY KEY, user TEXT COLLATE NOCASE,
                    msg TEXT, delivered INTEGER DEFAULT 0, payload BLOB);"""
            )
            connection.execute("CREATE INDEX tell_idx ON tell(user, delivered);")
            connection.execute(
                "INSERT INTO tell(user, msg, delivered, payload) VALUES (?,?,?,?);",
                ("alice", "héllo wörld", 1, b"\x00\xffbytes"),
            )
            connection.execute(
                "INSERT INTO tell(user, msg, delivered, payload) VALUES (?,?,?,?);",
                ("bob", None, 0, None),
            )
        connection.close()

    def test_round_trip_preserves_schema_and_rows(self) -> None:
        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir, "source.db")
            self._make_source(source)

            dump = StringIO()
            self.assertEqual(export_database(str(source), dump), 2)

            restored = Path(temp_dir, "restored.db")
            dump.seek(0)
            self.assertEqual(import_database(dump, str(restored)), 2)

            connection = sqlite3.connect(restored)
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            rows = connection.execute("SELECT * FROM tell ORDER BY id;").fetchall()
            self.assertEqual(
                [
                    (row["user"], row["msg"], row["delivered"], row["payload"])
                    for row in rows
                ],
                [("alice", "héllo wörld", 1, b"\x00\xffbytes"), ("bob", None, 0, None)],
            )
            index_names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index';"
                )
            }
            self.assertIn("tell_idx", index_names)
            # COLLATE NOCASE survived the schema round trip
            self.assertTrue(
                connection.execute("SELECT 1 FROM tell WHERE user='ALICE';").fetchone()
            )

    def test_cli_refuses_to_overwrite_without_force(self) -> None:
        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir, "source.db")
            self._make_source(source)
            dump_path = Path(temp_dir, "dump.jsonl")
            self.assertEqual(main(["export", str(source), str(dump_path)]), 0)
            self.assertEqual(main(["import", str(dump_path), str(source)]), 2)
            self.assertEqual(
                main(["import", str(dump_path), str(source), "--force"]), 0
            )
