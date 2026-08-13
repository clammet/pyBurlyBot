from html import escape
from os import chmod, replace
from pathlib import Path
from secrets import token_urlsafe
from stat import S_IRGRP, S_IROTH, S_IRUSR, S_IWUSR
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import unquote

from util import URLREGEX
from util.types import BotLike


PROVIDES = ("paste",)
OPTIONS = {
    "wwwroot": (str, "Web directory location for storing pastes.", "data/pastes/"),
    "url_prefix": (
        str,
        "Prefix of the web-facing URL, e.g. https://example.test/paste/.",
        "http://localhost/pastepls",
    ),
}

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>%s</title>
</head>
<body>
<h1>%s</h1>
%s
</body>
</html>
"""


def _html_paste(content: str, title: str) -> str:
    parts: list[str] = []
    last_end = 0
    for match in URLREGEX.finditer(content):
        start, end = match.span()
        parts.append(escape(content[last_end:start], quote=True))
        url = match.group()
        parts.append(
            '<a rel="nofollow noreferrer" href="%s">%s</a>'
            % (escape(url, quote=True), escape(unquote(url), quote=True))
        )
        last_end = end
    parts.append(escape(content[last_end:], quote=True))
    escaped_title = escape(title, quote=True)
    paragraphs = "".join("<p>%s</p>" % line for line in "".join(parts).splitlines())
    return TEMPLATE % (escaped_title, escaped_title, paragraphs)


def paste(
    content: str,
    bot: BotLike | None = None,
    title: str = "BurlyBot paste",
    **kwargs: Any,
) -> str:
    if bot is None:
        raise ValueError("selfpaste requires a bot context")
    root = Path(bot.getOption("wwwroot", module="selfpaste"))
    url_prefix = bot.getOption("url_prefix", module="selfpaste")
    if not url_prefix:
        raise ValueError("selfpaste url_prefix is empty")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise OSError("selfpaste wwwroot is not a directory")

    is_html = bool(URLREGEX.search(content))
    extension = "html" if is_html else "txt"
    rendered = _html_paste(content, title) if is_html else content
    target_name = "%s.%s" % (token_urlsafe(12), extension)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=root,
        prefix=".paste-",
        delete=False,
    ) as temporary:
        temporary.write(rendered)
        temporary.flush()
        temporary_path = Path(temporary.name)
    chmod(temporary_path, S_IRUSR | S_IWUSR | S_IRGRP | S_IROTH)
    replace(temporary_path, root / target_name)
    return "%s/%s" % (url_prefix.rstrip("/"), target_name)


def init(bot: BotLike) -> bool:
    root = Path(bot.getOption("wwwroot", module="selfpaste"))
    root.mkdir(parents=True, exist_ok=True)
    return root.is_dir()
