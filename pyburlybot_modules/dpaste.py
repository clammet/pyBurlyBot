from typing import Any
from urllib.request import urlopen
from urllib.error import URLError
from urllib.parse import urlencode

from traceback import print_exc

PROVIDES = ("paste",)

APIURL = "http://dpaste.com/api/v2/"

def paste(s: str, syntax: str="text", title: str="BurlyBot paste",
	poster: str="BurlyBot", expiry_days: int=1, **kwargs: Any) -> str | None:
	data = {
		"title" : title,
		"syntax" : syntax,
		"poster" : poster,
		"expiry_days" : expiry_days,
		"content" : s
	}
	try: 
		result = urlopen(APIURL, urlencode(data).encode("utf-8"))
		if result.geturl() == APIURL:
			return result.read().decode("utf-8").strip()
		else:
			return result.geturl()
	except URLError: print_exc()
	return None
