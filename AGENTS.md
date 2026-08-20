Do not implement any compatibility with old python. Use latest python. Never
add version-compatibility ranges in code, but dependencies ARE pinned exactly:
requirements*.in compile to hashed requirements*.txt lockfiles and Renovate
bumps the pins (see docs/dependencies.md). Edit the .in files, never the
compiled .txt output. ruff's target-version in pyproject.toml is held one
minor version behind the newest Python so ruff format/lint never rewrite code
into newest-only syntax; bump it (plus the Dockerfile base and CI
python-version) together when moving to a new Python minor.

Always run python, tests, ruff and mypy through the project venv:
.venv/bin/python (there is no system `python` on the dev machine).

API helper modules (googleapi, wordsapi, openweathermap_api, ...) own HTTP,
keys, and errors for their service and return parsed, typed structures
(tuples or frozen dataclasses), never raw JSON dicts. See "API helper
modules" in README for the stragglers still to remediate.