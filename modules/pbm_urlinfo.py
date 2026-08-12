# urlinfo module

# Using Requests because easier

from util import Mapping, fetchone, URLREGEX
from re import compile as recompile, IGNORECASE, DOTALL

from requests import head, get

from html import unescape

# (code - reason) content-type, encoding, size, serversoftware, redirect
HEAD_RPL = "(%s - %s) %s, %s%s bytes, %s%s"
TITLE_REGEX = recompile('<title>(.*?)</title>', IGNORECASE|DOTALL)

def seen_link(event, bot):
	match = event.regex_match
	pos = match.regs[0]
	url = match.string[pos[0]:pos[1]]
	bot.dbQuery("""INSERT OR REPLACE INTO urlinfo (source, url) 
		VALUES (?,?);""", (event.target, url))

def _getURL(event, dbQuery):
	row = dbQuery("""SELECT url FROM urlinfo 
							WHERE source=?;""", (event.target,), fetchone)
	if not row:
		return None
	return row['url']

def lasturl(event, bot):
	url = _getURL(event, bot.dbQuery)
	if not url:
		return bot.say("Haven't seen any URLs in here.")
	bot.say(url)

def headers(event, bot):
	""" head [URL]. If no argument is provided the headers of the last URL will be displayed. 
	Otherwise the title of the provided URL will be displayed."""
	if not event.argument:
		url = _getURL(event, bot.dbQuery)
		if not url:
			return bot.say("Haven't seen any URLs in here.")
	else:
		url = event.argument
		if not url.startswith("http"): url = "http://" + url
	
	resp = head(url)
	h = resp.headers
	ctype = h.get("content-type", "?;").split(";")[0]
	server = h.get("server", "?")
	size = int(h.get("content-length", 0))
	if "location" in h: location = " -> %s" % h["location"]
	else: location = ""
	encoding = "%s, " % resp.encoding if resp.encoding else ""
	bot.say(HEAD_RPL % (resp.status_code, resp.reason, ctype, encoding, size, server, location) )

def title(event, bot):
	""" title [URL]. If no argument is provided the title of the last URL will be displayed. 
	Otherwise the title of the provided URL will be displayed."""
	if not event.argument:
		url = _getURL(event, bot.dbQuery)
		if not url:
			return bot.say("Haven't seen any URLs in here.")
	else:
		url = event.argument
		if not url.startswith("http"): url = "http://" + url
	
	# do fancy stream and iter with requests
	resp = get(url, stream=True)
	# only if content-type is html though
	ctype = resp.headers.get("content-type", "?;").split(";")[0]
	if ctype == "text/html":
		m = None
		try: chunk = next(resp.iter_content(chunk_size=1024*10, decode_unicode=True)) # only get one chunk to look in (10KB)
		except StopIteration: bot.say("Couldn't find a title in (%s)." % url)
		else:
			if isinstance(chunk, bytes):
				chunk = chunk.decode(resp.encoding or "utf-8", "replace")
			m = TITLE_REGEX.search(chunk)
			if m: 
				title = m.group(1)
				bot.say("Title: %s" % unescape(title))
			else: bot.say("Couldn't find a title in (%s)." % url)
	else:
		# TODO: Maybe display last portion of pathname using something like os.path.basename
		bot.say("No title for (%s) type in (%s)." % (ctype, url))
	resp.close()

def init(bot):
	global GAPI_MODULE # oh nooooooooooooooooo
	
	bot.dbCheckCreateTable("urlinfo", 
		'''CREATE TABLE urlinfo(
			source TEXT PRIMARY KEY COLLATE NOCASE,
			url TEXT
		);''')
	GAPI_MODULE = bot.getModule("pbm_googleapi")
	return True

#mappings to methods
mappings = (Mapping(command=("head",), function=headers), Mapping(command=("title",), function=title), Mapping(command=("lasturl",), function=lasturl),
	Mapping(types=["privmsged"], regex=URLREGEX, function=seen_link),)
