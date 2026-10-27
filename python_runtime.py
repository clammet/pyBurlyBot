"""Keep Python settings aligned with the pinned Docker runtime."""

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys


@dataclass(frozen=True)
class PythonRuntime:
    version: str
    minor: str
    ruff_target: str


def read_runtime(root: Path) -> PythonRuntime:
    matches = re.findall(
        r"^FROM python:(\d+)\.(\d+)\.(\d+)-[\w.-]+@sha256:[a-f0-9]{64}$",
        (root / "Dockerfile").read_text(),
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise ValueError(
            "Dockerfile must contain one stable, digest-pinned Python image"
        )
    major, minor, patch = matches[0]
    if int(minor) == 0:
        raise ValueError("A new Python major requires reviewing the Ruff target policy")
    return PythonRuntime(
        f"{major}.{minor}.{patch}",
        f"{major}.{minor}",
        f"py{major}{int(minor) - 1}",
    )


def _replace(source: str, pattern: str, value: str, name: str) -> str:
    updated, count = re.subn(pattern, lambda match: match[1] + value, source)
    if count == 0:
        raise ValueError(f"Could not locate {name}")
    return updated


def expected_settings(root: Path, runtime: PythonRuntime) -> dict[str, str]:
    settings = {}
    for name in (
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "requirements.in",
        "requirements-dev.in",
        "docs/dependencies.md",
    ):
        source = (root / name).read_text()
        if name == ".github/workflows/ci.yml":
            source = _replace(
                source,
                r'(?m)^(\s*python-version: ")\d+\.\d+\.\d+(?=")',
                runtime.version,
                "CI Python version",
            )
        elif name == "pyproject.toml":
            source = _replace(
                source,
                r'(?m)^(target-version = ")py\d+(?=")',
                runtime.ruff_target,
                "Ruff target-version",
            )
        else:
            source = _replace(
                source,
                r"(--python-version[= ])\d+\.\d+\b",
                runtime.minor,
                f"{name} compilation target",
            )
            if name == "requirements.in":
                source = _replace(
                    source,
                    r"(ghcr.io/astral-sh/uv:python)\d+\.\d+(?=-)",
                    runtime.minor,
                    "documented compiler image",
                )
        settings[name] = source
    return settings


def _lockfile_target(root: Path, name: str) -> str:
    header = "\n".join((root / name).read_text().splitlines()[:3])
    matches = re.findall(r"--python-version=(\d+\.\d+)\b", header)
    if len(matches) != 1:
        raise ValueError(f"Could not locate {name} compilation target")
    return matches[0]


def compile_lockfiles(root: Path, runtime: PythonRuntime) -> None:
    uv = Path(sys.executable).with_name("uv")
    for name in ("requirements", "requirements-dev"):
        command = [
            str(uv),
            "pip",
            "compile",
            "--universal",
            "--generate-hashes",
            f"--python-version={runtime.minor}",
        ]
        if name == "requirements-dev":
            command.append("--constraint=requirements.txt")
        command.extend((f"--output-file={name}.txt", f"{name}.in"))
        subprocess.run(command, cwd=root, check=True, stdout=subprocess.DEVNULL)


def synchronize(root: Path, *, write: bool = False) -> list[str]:
    runtime = read_runtime(root)
    settings = expected_settings(root, runtime)
    changed = [
        name
        for name, expected in settings.items()
        if (root / name).read_text() != expected
    ]
    lockfiles = [
        name
        for name in ("requirements.txt", "requirements-dev.txt")
        if _lockfile_target(root, name) != runtime.minor
    ]
    if not write:
        return changed + lockfiles

    format_python = "pyproject.toml" in changed
    for name in changed:
        (root / name).write_text(settings[name])
    if lockfiles:
        compile_lockfiles(root, runtime)
    if format_python:
        subprocess.run(
            [sys.executable, "-m", "ruff", "format", "."], cwd=root, check=True
        )
    remaining = synchronize(root)
    if remaining:
        raise ValueError(
            "Python settings are still inconsistent: " + ", ".join(remaining)
        )
    return changed + lockfiles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="Check settings without edits"
    )
    mode.add_argument("--write", action="store_true", help="Synchronize and regenerate")
    args = parser.parse_args()
    try:
        changed = synchronize(Path(__file__).resolve().parent, write=args.write)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Python runtime synchronization failed: {error}", file=sys.stderr)
        return 1
    if changed and not args.write:
        print("Python settings disagree with Dockerfile: " + ", ".join(changed))
        print("Run .venv/bin/python python_runtime.py --write to synchronize them.")
        return 1
    print("Python runtime settings are consistent with Dockerfile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
