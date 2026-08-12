from typing import Any
from util.types import BotLike
# selfpaste
# *NIX ONLY. (Unless you code up the file inode part for the win32 side...)

# used hashid because lazy

# TODO: probably not very secure. Probably vunerable to 100 JS injection things.

# need cron file that cleans up stale things that's run from cron

from tempfile import NamedTemporaryFile
from os.path import exists, join
from os import chmod, makedirs, stat, rename
from errno import EEXIST
from stat import S_IRUSR, S_IWUSR, S_IRGRP, S_IWGRP, S_IROTH
from html import escape
from urllib.parse import unquote

from hashids import Hashids
hashids = Hashids()

from util import URLREGEX

PROVIDES = ("paste",)

OPTIONS = {
	"wwwroot" : (str, "Web directory location for storing pastes.", "data/pastes/"),
	"url_prefix" : (str, "Prefix of the webfacing URL. e.g. 'http://domain.com/paste/'", "http://localhost/pastepls"),
}

# tempfile.NamedTemporaryFile  dir= module/server path for www. prefix=tmp
# after file has been got, get it's inode number, write to file, then mode to hex(inode)

TEMPLATE = """<!DOCTYPE html>
<html>
<head>
	<meta charset="utf-8" />
	<title>%s</title>
	<link href='https://fonts.googleapis.com/css?family=Oxygen+Mono' rel='stylesheet' type='text/css'>
	<link rel="stylesheet" href="style/style.css">
</head>
<body>
<h3>%s</h3>
%s
</body>
</html>
"""

# TODO: Do we need to define some sort of 'typical paste API'?
def paste(s: str, bot: BotLike | None=None, title: str="BurlyBot paste",
	**kwargs: Any) -> str:
	assert(bot is not None)
	wwwroot = bot.getOption("wwwroot", module="selfpaste")
	urlprefix = bot.getOption("url_prefix", module="selfpaste")
	assert(wwwroot and urlprefix)
	if not exists(wwwroot):
		try: makedirs(wwwroot)
		except OSError as e:
			if e.errno != EEXIST:
				return "PASTE ERROR: Cannot access wwwroot"
		
	tempfile = NamedTemporaryFile(mode='w+b', dir=wwwroot, delete=False)
	
	nf = "%s.%%s" % hashids.encode(stat(tempfile.name).st_ino)
	if "http" in s:
		# linkify stuff.
		# more tedious than I thought it would be... process each line, and cut out the surrounding nonlink text to escape
		title = escape(title, quote=True)
		lastend = 0
		parts = []
		for match in URLREGEX.finditer(s):
			mstart, mend = match.span()
			parts.append(escape(s[lastend:mstart], quote=True))
			m = match.group()
			#ms = m.split("://", 1)
			# Assume generated URLs are already encoded properly, only need to htmlencode them
			parts.append('<a href="%s">%s</a>' % (escape(m, quote=True), escape(unquote(m), quote=True)))
			lastend = mend
		parts.append(escape(s[lastend:], quote=True))
		s = TEMPLATE % (title, title, "".join(("<p>%s</p>" % x for x in "".join(parts).split("\n"))))
		nf = nf % "html"
	else:
		nf = nf % "txt"
	tempfile.write(s.encode("utf-8"))
	tempfile.close()
	chmod(tempfile.name, S_IRUSR|S_IWUSR|S_IRGRP|S_IWGRP|S_IROTH) # 664
	rename(tempfile.name, join(wwwroot, nf))
	return "%s/%s" % (urlprefix.rstrip("/"), nf)
	
	
def init(bot: BotLike) -> bool:
	# TODO: maybe check if wwwroot is writable?
	return True
