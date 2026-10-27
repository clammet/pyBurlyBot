from pathlib import Path
import re
from shutil import copy2
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
from unittest import TestCase
from unittest.mock import patch

from python_runtime import (
    PythonRuntime,
    compile_lockfiles,
    read_runtime,
    synchronize,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PythonRuntimeTest(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in (
            "Dockerfile",
            ".github/workflows/ci.yml",
            "pyproject.toml",
            "requirements.in",
            "requirements-dev.in",
            "requirements.txt",
            "requirements-dev.txt",
            "docs/dependencies.md",
        ):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            copy2(PROJECT_ROOT / name, target)
        # Fixtures stay independent of whichever Python minor the project runs.
        (self.root / "Dockerfile").write_text(
            "FROM python:3.14.7-slim-trixie@sha256:" + "a" * 64 + "\n"
        )
        self._compile(self.root, PythonRuntime("3.14.7", "3.14", "py313"))
        with patch("python_runtime.subprocess.run"):
            synchronize(self.root, write=True)

    def _compile(self, root: Path, runtime: PythonRuntime) -> None:
        for name in ("requirements.txt", "requirements-dev.txt"):
            path = root / name
            path.write_text(
                re.sub(
                    r"--python-version=\d+\.\d+",
                    f"--python-version={runtime.minor}",
                    path.read_text(),
                )
            )

    def _set_version(self, version: str) -> None:
        (self.root / "Dockerfile").write_text(
            f"FROM python:{version}-slim-trixie@sha256:" + "b" * 64 + "\n"
        )

    def test_consistent_settings_require_no_edits_or_commands(self) -> None:
        with patch("python_runtime.subprocess.run") as run:
            self.assertEqual(synchronize(self.root), [])
            self.assertEqual(synchronize(self.root, write=True), [])
        run.assert_not_called()

    def test_ci_only_upgrade_is_rejected_without_changing_files(self) -> None:
        path = self.root / ".github/workflows/ci.yml"
        source = path.read_text().replace(
            'python-version: "3.14.7"', 'python-version: "3.15.1"'
        )
        path.write_text(source)
        self.assertEqual(synchronize(self.root), [".github/workflows/ci.yml"])
        self.assertEqual(path.read_text(), source)

    def test_minor_upgrade_synchronizes_settings_and_regenerates_locks(self) -> None:
        self._set_version("3.15.1")
        self.assertIn("requirements.txt", synchronize(self.root))
        with (
            patch(
                "python_runtime.compile_lockfiles", side_effect=self._compile
            ) as compile,
            patch("python_runtime.subprocess.run") as run,
        ):
            synchronize(self.root, write=True)
            compile.assert_called_once_with(
                self.root, PythonRuntime("3.15.1", "3.15", "py314")
            )
            run.assert_called_once_with(
                [sys.executable, "-m", "ruff", "format", "."],
                cwd=self.root,
                check=True,
            )
            self.assertEqual(synchronize(self.root), [])
            self.assertEqual(synchronize(self.root, write=True), [])
            compile.assert_called_once()
            run.assert_called_once()
        configuration = tomllib.loads((self.root / "pyproject.toml").read_text())
        self.assertEqual(configuration["tool"]["ruff"]["target-version"], "py314")
        self.assertIn(
            'python-version: "3.15.1"',
            (self.root / ".github/workflows/ci.yml").read_text(),
        )

    def test_skipped_minor_upgrades_derive_the_correct_ruff_target(self) -> None:
        self._set_version("3.17.2")
        with (
            patch("python_runtime.compile_lockfiles", side_effect=self._compile),
            patch("python_runtime.subprocess.run"),
        ):
            synchronize(self.root, write=True)
        self.assertEqual(read_runtime(self.root).ruff_target, "py316")
        self.assertEqual(synchronize(self.root), [])

    def test_patch_upgrade_keeps_the_locks_and_ruff_target(self) -> None:
        self._set_version("3.14.8")
        with patch("python_runtime.subprocess.run") as run:
            self.assertEqual(
                synchronize(self.root, write=True), [".github/workflows/ci.yml"]
            )
        run.assert_not_called()
        self.assertEqual(synchronize(self.root), [])

    def test_compile_uses_hashes_new_target_and_runtime_constraint_in_order(
        self,
    ) -> None:
        with patch("python_runtime.subprocess.run") as run:
            compile_lockfiles(self.root, PythonRuntime("3.15.1", "3.15", "py314"))
        self.assertEqual(run.call_count, 2)
        runtime, development = (call.args[0] for call in run.call_args_list)
        for command in (runtime, development):
            self.assertIn("--universal", command)
            self.assertIn("--generate-hashes", command)
            self.assertIn("--python-version=3.15", command)
        self.assertEqual(
            runtime[-2:], ["--output-file=requirements.txt", "requirements.in"]
        )
        self.assertIn("--constraint=requirements.txt", development)
        self.assertEqual(
            development[-2:],
            ["--output-file=requirements-dev.txt", "requirements-dev.in"],
        )

    def test_failed_compile_prevents_a_successful_synchronization(self) -> None:
        self._set_version("3.15.1")
        with (
            patch(
                "python_runtime.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "uv"),
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            synchronize(self.root, write=True)
        self.assertIn("requirements.txt", synchronize(self.root))

    def test_unpinned_or_prerelease_images_require_review(self) -> None:
        for image in (
            "python:3.15.1-slim-trixie",
            "python:3.16.0a1-slim-trixie@sha256:" + "a" * 64,
            "python:4.0.0-slim-trixie@sha256:" + "a" * 64,
        ):
            with self.subTest(image=image):
                (self.root / "Dockerfile").write_text(f"FROM {image}\n")
                with self.assertRaises(ValueError):
                    synchronize(self.root, write=True)
