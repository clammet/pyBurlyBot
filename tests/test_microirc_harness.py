import json
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from microirc_harness import (
    DEFAULT_TEMPLATE,
    EXCLUDED_HARNESS_MODULES,
    PROJECT_ROOT,
    harness_target,
    load_config,
    overlay_api_credentials,
    write_runtime_config,
)


class MicroIRCHarnessTest(TestCase):
    def test_template_selects_all_non_excluded_modules_without_secrets(self) -> None:
        config = load_config(DEFAULT_TEMPLATE)
        configured = set(config["modules"])
        available = {
            path.stem
            for path in (PROJECT_ROOT / "pyburlybot_modules").glob("*.py")
            if path.stem != "__init__"
        }

        self.assertEqual(configured, available - set(EXCLUDED_HARNESS_MODULES))
        self.assertEqual(
            harness_target(config).host,
            "127.0.0.1",
        )
        for options in config["moduleopts"].values():
            for name, value in options.items():
                if name in {"API_KEY", "CSE_ID"}:
                    self.assertEqual(value, "")

    def test_api_overlay_copies_only_explicit_api_options(self) -> None:
        config = {
            "moduleopts": {
                "calc": {"API_KEY": ""},
                "googleapi": {"API_KEY": "", "CSE_ID": ""},
                "service": {"password": "", "TOKEN": ""},
            }
        }
        secrets = {
            "moduleopts": {
                "calc": {"API_KEY": "calc-secret"},
                "googleapi": {
                    "API_KEY": "google-secret",
                    "CSE_ID": "search-id",
                },
                "service": {"password": "password", "TOKEN": "token"},
            }
        }

        imported = overlay_api_credentials(config, secrets)

        self.assertEqual(
            imported,
            ("calc.API_KEY", "googleapi.API_KEY", "googleapi.CSE_ID"),
        )
        self.assertEqual(config["moduleopts"]["calc"]["API_KEY"], "calc-secret")
        self.assertEqual(config["moduleopts"]["service"]["password"], "")
        self.assertEqual(config["moduleopts"]["service"]["TOKEN"], "")

    def test_runtime_config_is_private_and_redirects_writable_state(self) -> None:
        config = load_config(DEFAULT_TEMPLATE)
        original = deepcopy(config)
        target = harness_target(config, port_override=12345)
        with TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)

            config_path = write_runtime_config(config, runtime_dir, target)
            runtime = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertEqual(runtime["datadir"], str(runtime_dir / "data"))
            self.assertEqual(
                runtime["moduleopts"]["logindexsearch"]["indexdir"],
                str(runtime_dir / "logindex"),
            )
            self.assertEqual(
                runtime["moduleopts"]["selfpaste"]["wwwroot"],
                str(runtime_dir / "pastes"),
            )
            self.assertEqual(runtime["servers"][0]["port"], 12345)
        self.assertEqual(config, original)

    def test_harness_wires_real_bot_through_relay_and_client(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "microirc_harness.py"),
                "--port",
                "0",
                "--no-secrets",
                "--command",
                "md5 microIRC",
                "--startup-timeout",
                "20",
                "--reply-timeout",
                "3",
                "--shutdown-timeout",
                "8",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=40,
        )

        diagnostic = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, diagnostic)
        self.assertIn("Bot joined; starting microIRC command client", diagnostic)
        self.assertIn("1094244a08cd0fe22be8fdb10bde204f", diagnostic)
