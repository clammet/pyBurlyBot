from json import dumps, load
from pathlib import Path
import sqlite3
from os import chmod
from stat import S_IMODE
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import TestCase

from util.db import DBaccess
from util.moduleloader import ModuleRegistry
from util.settings import BaseServer, ConfigException, Server, SettingsBase


class SettingsTest(TestCase):
    def test_save_options_writes_utf8_json_in_text_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "BurlyBot.json")
            settings = SettingsBase()
            settings.configfile = str(config_path)
            settings.nick = "BürlyBot"

            settings.saveOptions()

            with config_path.open(encoding="utf-8") as config_file:
                config = load(config_file)
            self.assertEqual(config["nick"], "BürlyBot")
            self.assertEqual(len(config["servers"]), 2)

    def test_server_normalizes_python_3_config_values(self) -> None:
        server = BaseServer(
            {
                "serverlabel": "example",
                "host": "irc.example.net",
                "port": "+6697",
                "channels": ["#one", ["#two", "secret"]],
                "admins": ["Alice"],
            }
        )

        self.assertEqual(server.port, 6697)
        self.assertTrue(server.ssl)
        self.assertEqual(server.channels, [("#one",), ("#two", "secret")])
        self.assertEqual(server._admins, ["alice"])

    def test_server_requires_a_host(self) -> None:
        with self.assertRaises(ConfigException):
            BaseServer({"serverlabel": "missing-host"})

    def test_server_reload_removes_explicit_values(self) -> None:
        server = BaseServer(
            {
                "serverlabel": "example",
                "host": "irc.example.net",
                "admins": ["Alice"],
                "nickservpass": "old-secret",
                "verify": False,
            }
        )
        server.setup(
            {"serverlabel": "example", "host": "irc.example.net", "verify": True}
        )
        self.assertNotIn("_admins", server.__dict__)
        self.assertNotIn("nickservpass", server.__dict__)
        self.assertTrue(server.__dict__["verify"])

    def test_invalid_reload_does_not_mutate_live_settings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "BurlyBot.json")
            config_path.write_text(
                dumps(
                    {
                        "nick": "Stable",
                        "servers": [
                            {
                                "serverlabel": "stable-network",
                                "host": "irc.example.net",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            settings = SettingsBase()
            settings.configfile = str(config_path)
            settings.reloadStage1()

            config_path.write_text(
                dumps(
                    {
                        "nick": "PartiallyApplied",
                        "servers": [{"host": "missing-label.example.net"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigException):
                settings.reloadStage1()

            self.assertEqual(settings.nick, "Stable")
            self.assertEqual(set(settings.servers), {"stable-network"})

    def test_config_options_are_type_checked(self) -> None:
        base = {"serverlabel": "typed", "host": "irc.example.net"}
        with self.assertRaises(ConfigException):
            BaseServer({**base, "channels": "#chan"})  # iterates char-by-char
        with self.assertRaises(ConfigException):
            BaseServer({**base, "channels": ["#ok", 5]})
        with self.assertRaises(ConfigException):
            BaseServer({**base, "admins": "clam"})
        with self.assertRaises(ConfigException):
            BaseServer({"serverlabel": "typed", "host": 6667})
        # altnicks accepts a bare string (documented coercion)
        server = BaseServer({**base, "altnicks": "alt1"})
        self.assertEqual(server.altnicks, ["alt1"])

    def test_main_config_options_are_type_checked(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "BurlyBot.json")
            config_path.write_text(
                dumps({"debug": True, "servers": []}), encoding="utf-8"
            )
            settings = SettingsBase()
            settings.configfile = str(config_path)
            with self.assertRaises(ConfigException):
                settings.reloadStage1()

    def test_module_reload_re_arms_signedon_when_connected(self) -> None:
        server = Server({"serverlabel": "reload-test", "host": "irc.example.org"})
        posted: list[str] = []
        cast(Any, server.container).postEvent = lambda event_type, **kw: posted.append(
            event_type
        )
        registry = ModuleRegistry()

        server.reload_modules(registry)
        self.assertEqual(posted, [])  # not connected: real signedon comes later

        server.container._botinst = cast(Any, object())
        server.reload_modules(registry)
        self.assertEqual(posted, ["signedon"])

    def test_module_list_is_ordered_and_deduplicated(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "BurlyBot.json")
            config_path.write_text(
                dumps({"modules": ["words", "help", "words"], "servers": []}),
                encoding="utf-8",
            )
            settings = SettingsBase()
            settings.configfile = str(config_path)
            settings.reloadStage1()
            self.assertEqual(settings.modules, ["words", "help"])

    def test_admins_require_an_authenticated_account_by_default(self) -> None:
        server = BaseServer(
            {
                "serverlabel": "secure",
                "host": "irc.example.net",
                "admins": ["Alice"],
            }
        )
        self.assertTrue(server.is_admin("DifferentNick", "alice"))
        self.assertFalse(server.is_admin("Alice", None))

    def test_insecure_admin_fallback_is_explicit_and_warns(self) -> None:
        server = BaseServer(
            {
                "serverlabel": "legacy",
                "host": "irc.example.net",
                "admins": ["Alice"],
                "insecure": True,
            }
        )
        with self.assertLogs("util.settings", "WARNING") as warning:
            server.warn_insecure_auth()
        self.assertTrue(server.is_admin("ALICE", None))
        self.assertIn("nickname-only", "\n".join(warning.output))

    def test_sasl_credentials_must_be_configured_as_a_pair(self) -> None:
        with self.assertRaises(ConfigException):
            BaseServer(
                {
                    "serverlabel": "broken",
                    "host": "irc.example.net",
                    "sasl_username": "bot-account",
                }
            )

    def test_sasl_plain_requires_tls(self) -> None:
        with self.assertRaises(ConfigException):
            BaseServer(
                {
                    "serverlabel": "plaintext",
                    "host": "irc.example.net",
                    "sasl_username": "bot-account",
                    "sasl_password": "secret",
                }
            )

    def test_save_options_is_private_atomic_and_corrects_existing_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "BurlyBot.json")
            settings = SettingsBase()
            settings.configfile = str(config_path)
            settings.saveOptions()
            self.assertEqual(S_IMODE(config_path.stat().st_mode), 0o600)

            chmod(config_path, 0o640)
            settings.nick = "Replacement"
            settings.saveOptions()
            self.assertEqual(S_IMODE(config_path.stat().st_mode), 0o600)
            with config_path.open(encoding="utf-8") as config_file:
                self.assertEqual(load(config_file)["nick"], "Replacement")
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_database_worker_uses_python_3_thread_api(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = DBaccess(temp_dir, "test.db")
            self.assertEqual(S_IMODE(Path(temp_dir, "test.db").stat().st_mode), 0o600)
            database.start()
            try:
                database.query("CREATE TABLE values_test (value TEXT)")
                database.query("INSERT INTO values_test VALUES (?)", ("hello",))
                rows = database.query("SELECT value FROM values_test")
                self.assertEqual(rows[0]["value"], "hello")
            finally:
                database.stop()

    def test_database_batch_rolls_back_on_first_failure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = DBaccess(temp_dir, "test.db")
            database.start()
            try:
                database.query("CREATE TABLE values_test (value TEXT)")
                with self.assertRaises(sqlite3.Error):
                    database.batch(
                        (
                            ("INSERT INTO values_test VALUES (?)", ("lost",)),
                            ("INSERT INTO missing_table VALUES (?)", ("failure",)),
                        )
                    )
                self.assertEqual(database.query("SELECT * FROM values_test"), [])
            finally:
                database.stop()
