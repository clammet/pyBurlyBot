Do not implement any compatibility with old python. Use latest python and
avoid version pins wherever possible. The one deliberate exception is ruff's
target-version in pyproject.toml, held one minor version behind the newest
Python so ruff format/lint never rewrite code into newest-only syntax.

Always run python, tests, ruff and mypy through the project venv:
.venv/bin/python (there is no system `python` on the dev machine).