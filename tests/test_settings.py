from json import load
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from util.db import DBaccess
from util.settings import BaseServer, ConfigException, SettingsBase


class SettingsTest(TestCase):
	def test_save_options_writes_utf8_json_in_text_mode(self):
		with TemporaryDirectory() as temp_dir:
			config_path = Path(temp_dir, "BurlyBot.json")
			settings = SettingsBase()
			settings.configfile = config_path
			settings.nick = "BürlyBot"

			settings.saveOptions()

			with config_path.open(encoding="utf-8") as config_file:
				config = load(config_file)
			self.assertEqual(config["nick"], "BürlyBot")
			self.assertEqual(len(config["servers"]), 2)

	def test_server_normalizes_python_3_config_values(self):
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

	def test_server_requires_a_host(self):
		with self.assertRaises(ConfigException):
			BaseServer({"serverlabel": "missing-host"})

	def test_database_worker_uses_python_3_thread_api(self):
		with TemporaryDirectory() as temp_dir:
			database = DBaccess(temp_dir, "test.db")
			database.start()
			try:
				database.query("CREATE TABLE values_test (value TEXT)")
				database.query("INSERT INTO values_test VALUES (?)", ("hello",))
				rows = database.query("SELECT value FROM values_test")
				self.assertEqual(rows[0]["value"], "hello")
			finally:
				database.stop()
