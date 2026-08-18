Do not implement any compatibility with old python. Use latest python and
avoid version pins wherever possible. The one deliberate exception is ruff's
target-version in pyproject.toml, held one minor version behind the newest
Python so ruff format/lint never rewrite code into newest-only syntax.

Always run python, tests, ruff and mypy through the project venv:
.venv/bin/python (there is no system `python` on the dev machine).

API helper modules (googleapi, wordsapi, openweathermap_api, ...) own HTTP,
keys, and errors for their service and return parsed, typed structures
(tuples or frozen dataclasses), never raw JSON dicts. See "API helper
modules" in README for the stragglers still to remediate.