"""Export/import pyBurlyBot SQLite databases as JSON Lines.

Interchange format (one JSON object per line):
    {"type": "schema", "name": ..., "sql": "CREATE ..."}
    {"type": "row", "table": ..., "values": {column: value, ...}}
BLOB values are wrapped as {"__blob__": "<base64>"}.

Made for server migration (#73): dump on the old host, load into a fresh
database on the new one, or keep the dump as an archive.

    python dbexport.py export data/BurlyBot.db dump.jsonl
    python dbexport.py import dump.jsonl data/BurlyBot.db
"""

from argparse import ArgumentParser
from base64 import b64decode, b64encode
from collections.abc import Iterable
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, TextIO


def _encode_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__blob__": b64encode(value).decode("ascii")}
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and "__blob__" in value:
        return b64decode(value["__blob__"])
    return value


def _quote_identifier(name: str) -> str:
    return '"%s"' % name.replace('"', '""')


def export_database(database: str, out: TextIO) -> int:
    """Write every schema entry and table row of `database` to `out`.
    Returns the number of rows exported."""
    connection = sqlite3.connect("file:%s?mode=ro" % database, uri=True)
    connection.row_factory = sqlite3.Row
    rows = 0
    try:
        # tables first so an import can run the dump top to bottom
        schemas = connection.execute(
            "SELECT name, sql FROM sqlite_master"
            " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            " ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name;"
        ).fetchall()
        for entry in schemas:
            out.write(
                json.dumps(
                    {"type": "schema", "name": entry["name"], "sql": entry["sql"]}
                )
                + "\n"
            )
        tables = connection.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
        ).fetchall()
        for table in tables:
            name = table["name"]
            statement = "SELECT * FROM %s;" % _quote_identifier(name)  # noqa: S608 - identifier from sqlite_master, quoted
            for row in connection.execute(statement):
                values = {key: _encode_value(row[key]) for key in row.keys()}
                out.write(
                    json.dumps({"type": "row", "table": name, "values": values}) + "\n"
                )
                rows += 1
    finally:
        connection.close()
    return rows


def import_database(dump: Iterable[str], database: str) -> int:
    """Load a dump produced by export_database into `database` (one
    transaction). Returns the number of rows imported."""
    connection = sqlite3.connect(database)
    rows = 0
    try:
        with connection:
            for raw_line in dump:
                line = raw_line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry["type"] == "schema":
                    connection.execute(entry["sql"])
                elif entry["type"] == "row":
                    values = {
                        key: _decode_value(value)
                        for key, value in entry["values"].items()
                    }
                    columns = ", ".join(_quote_identifier(key) for key in values)
                    marks = ", ".join(["?"] * len(values))
                    statement = "INSERT INTO %s (%s) VALUES (%s);" % (  # noqa: S608 - identifiers from the dump, quoted
                        _quote_identifier(entry["table"]),
                        columns,
                        marks,
                    )
                    connection.execute(statement, tuple(values.values()))
                    rows += 1
                else:
                    raise ValueError("Unknown dump entry type: %r" % entry["type"])
    finally:
        connection.close()
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        description="Export/import pyBurlyBot SQLite databases as JSON Lines."
    )
    actions = parser.add_subparsers(dest="action", required=True)
    exporter = actions.add_parser("export", help="dump a database to JSON Lines")
    exporter.add_argument("database")
    exporter.add_argument("dump", nargs="?", help="output file (default: stdout)")
    importer = actions.add_parser(
        "import", help="load a JSON Lines dump into a new database"
    )
    importer.add_argument("dump")
    importer.add_argument("database")
    importer.add_argument(
        "--force", action="store_true", help="replace an existing database file"
    )
    args = parser.parse_args(argv)

    if args.action == "export":
        if not Path(args.database).exists():
            print("Error: database (%s) not found." % args.database, file=sys.stderr)
            return 2
        if args.dump:
            with open(args.dump, "w", encoding="utf-8") as out:
                rows = export_database(args.database, out)
            print("Exported %d rows to %s." % (rows, args.dump), file=sys.stderr)
        else:
            export_database(args.database, sys.stdout)
        return 0

    target = Path(args.database)
    if target.exists():
        if not args.force:
            print(
                "Error: (%s) exists. Use --force to replace it." % args.database,
                file=sys.stderr,
            )
            return 2
        target.unlink()
    with open(args.dump, encoding="utf-8") as dump:
        rows = import_database(dump, args.database)
    print("Imported %d rows into %s." % (rows, args.database), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
