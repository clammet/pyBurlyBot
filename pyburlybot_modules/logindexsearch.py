from datetime import datetime
from multiprocessing.connection import Connection
from typing import Any, TypeAlias
from util.event import Event
from util.types import BotLike
#logging indexing thing

from whoosh.index import create_in, open_dir, exists_in
from whoosh.fields import DATETIME, Schema, TEXT, NUMERIC
from whoosh.qparser import QueryParser
from whoosh.query import Term
from whoosh.sorting import Count, FieldFacet

from multiprocessing import Process, Pipe

from threading import current_thread, Thread

from queue import Queue, Empty

from collections import deque

from os.path import exists, join
from os import makedirs

from re import compile as re_compile

from sys import stdout

from traceback import print_exc

from twisted.words.protocols.irc import CHANNEL_PREFIXES

from util import Mapping, argumentSplit, functionHelp, pastehelper
# TODO: perhaps put writer in a thread inside the new process while the batch-write is happening
#		so searches and log buffer can still be done while writing instead of filling up the interprocess
#		pipe/queue/socket/whatever while the writer is blocking

SCHEMA = Schema(id=NUMERIC(numtype=int, bits=64, stored=True, unique=True), timestamp=DATETIME(sortable=True, stored=True), 
	nick=TEXT(stored=True), user=TEXT(stored=True), source=TEXT(stored=True), content=TEXT(stored=True))

OPTIONS = {
	"indexdir" : (str, "Dir where log indexes are stored.", "logindex"),
}
REQUIRES = ("users",)
USERS_MODULE: Any = None

SOURCE_REGEX = re_compile(r".*\bsource:.")
NICK_REGEX = re_compile(r".*\bnick:(.+)/b")

BUFFERLINES = 100
# Should maybe store a timestamp in IndexProxy.waiting so we can use the following to check if there's any stale threads hanging.
BAD_TIMEOUT = 120

# CONST/IDENTIFIERS
QUERY = 0
LOG = 1
RENAME = 2
STOP = -1

LOG_FORMAT = "<%s> %s" # <nick> msg

LogEntry: TypeAlias = tuple[datetime, str, str | None, str, str]
SearchResult: TypeAlias = tuple[Any, ...]
SearchResults: TypeAlias = list[SearchResult] | None


def prnt(s: object) -> None:
	print(s)
	stdout.flush()

class IndexProcess(Process):
	def __init__(self, network: str, indexdir: str, indexp: Connection) -> None:
		super().__init__()
		self.index_p = indexp
		self.indexdir = join(indexdir, network)
		self.network = network
		self.buffer: deque[LogEntry] = deque(maxlen=BUFFERLINES)
		self.ix: Any = None
		self.qp: Any = None
		self.searcher: Any = None
		
	# timestamp, nick, source, msg
	def _processLog(self, args: LogEntry) -> None:
		buffer = self.buffer
		buffer.append(args)
		if len(buffer) == BUFFERLINES:
			self._dumpBuffer(buffer)
			self.searcher = self.searcher.refresh()
			
	def _dumpBuffer(self, buffer: deque[LogEntry]) -> None:
		id = self.ix.reader().doc_count()
		with self.ix.writer() as iw:
			# dump buffer
			while True:
				try: 
					data = buffer.popleft() # timestamp, nick, user, source, msg
					iw.add_document(id=id, timestamp=data[0], nick=data[1], user=data[2], source=data[3], content=data[4])
				except IndexError: break
				except Exception:
					print_exc()
					prnt("EXCEPTION IN LOGGER")
				else:
					id += 1
					
	def _processRename(self, data: tuple[str, str]) -> None:
		old, new = data
		self._dumpBuffer(self.buffer)
		self.searcher = self.ix.searcher()
		results = self.searcher.search(Term('user', old.lower()), limit=None)
		with self.ix.writer() as iw:
			# dump buffer
			for hit in results:
				try: 
					iw.update_document(id=hit['id'], timestamp=hit["timestamp"], 
						nick=hit["nick"], user=new, source=hit["source"], content=hit["content"])
				except Exception:
					print_exc()
					prnt("EXCEPTION IN RENAME")
		self.searcher = self.ix.searcher()

	# threadident, source, query
	def _processSearch(
		self, data: tuple[int | None, str, str, int | None, str | None]
	) -> None:
		try:
			threadident, source, query, n, gb = data
			qp = self.qp.parse(query)
			results: list[SearchResult] = []
			if not SOURCE_REGEX.match(query): qp = qp & Term("source", source.lstrip(CHANNEL_PREFIXES).lower())
			if not gb:
				for item in self.searcher.search(qp, limit=n, groupedby=gb):
					results.append((item["timestamp"], item["nick"], item["source"], item["content"]))
			else:
				for user, count in self.searcher.search(qp, groupedby=FieldFacet(gb, maptype=Count)).groups().items():
					results.append((count, user))
		except Exception:
			self.index_p.send((threadident, None)) # pass None back to caller so user error can be displayed.
			print_exc()
			prnt("EXCEPTION IN SEARCH")
		else:
			self.index_p.send((threadident, results))
		
	def run(self) -> None:
		# open index
		self.buffer = deque(maxlen=BUFFERLINES)
		if not exists(self.indexdir):
			makedirs(self.indexdir)
			self.ix = create_in(self.indexdir, SCHEMA)
		else:
			if exists_in(self.indexdir): self.ix = open_dir(self.indexdir)
			else: self.ix = create_in(self.indexdir, SCHEMA)
		self.qp = QueryParser("content", self.ix.schema)
		self.searcher = self.ix.searcher()
		index_p = self.index_p
		while True:
			try:
				# check index_p
				try:
					type, data = index_p.recv()
				except EOFError: break
				try:
					if type == QUERY: self._processSearch(data)
					elif type == LOG: self._processLog(data)
					elif type == RENAME: self._processRename(data)
					else:
						prnt("Unexpected data in logindexsearch.")
				except Exception:
					print_exc()
					prnt("EXCEPTION in logindexsearch process.")
			except KeyboardInterrupt:
				break
		self._dumpBuffer(self.buffer)
		self.searcher.close()
		self.ix.close()	

class IndexProxy(Thread):
	def __init__(self, network: str, indexdir: str, cmdprefix: str) -> None:
		super().__init__()
		self.module_p, index_p = Pipe()
		self.proc = IndexProcess(network, indexdir, index_p)
		self.proc.start()
		self.inqueue: Queue[tuple[int, Any]] = Queue() # thread.ident, query/data
		self.waiting: dict[int | None, Queue[SearchResults]] = {} #threadID : queue
		self.cmdprefix = cmdprefix
		
	def run(self) -> None:
		procpipe = self.module_p
		while True:
			# process module calls
			try: type, data = self.inqueue.get(timeout=0.2)
			except Empty: pass
			else:
				try:
					#process queued item
					if type == STOP:
						self.module_p.close()
						break
					elif type == QUERY:	
						resq, threadident = data[0:2]
						data = data[1:]
						self.waiting[threadident] = resq
						procpipe.send((type, data))
					else:
						procpipe.send((type, data))
				except Exception:
					print_exc()
					prnt("IndexProxy Exception in pump.")
			# process pipe data
			while procpipe.poll():
				tid, result = procpipe.recv()
				try: 
					self.waiting.pop(tid).put(result)
				except KeyError:
					prnt("WAITING THREAD ID NOT FOUND FOR RESULT:"+repr(result))
		for queue in self.waiting.values():
			queue.put(None)
	
	def search(self, source: str, query: str, n: int | None,
		gb: str | None=None) -> SearchResults:
		""" Will return None if shutdown before response ready."""
		resultq: Queue[SearchResults] = Queue()
		self.inqueue.put((QUERY, (resultq, current_thread().ident, source, query, n, gb)))
		return resultq.get()
		
	def logmsg(self, timestamp: datetime, nick: str, user: str | None,
		source: str, message: str) -> None:
		# Ignore all lines that start with commandprefix, but allow things like "... (etc)"
		if message.startswith(self.cmdprefix) and not message.startswith(self.cmdprefix * 2): return
		self.inqueue.put((LOG, (timestamp, nick, user, source, message)))
		
	def stop(self) -> None:
		self.inqueue.put((STOP, None))
	
	# old, new
	def rename(self, *args: str) -> None:
		self.inqueue.put((RENAME, args))

INDEX_PROXIES: dict[str, IndexProxy] = {}

def logmsg(event: Event, bot: BotLike) -> None:
	# pass msg on to logger
	iproxy = INDEX_PROXIES.get(bot.network)
	if iproxy and event.nick is not None and event.target is not None and event.msg is not None:
		user = USERS_MODULE.get_username(bot, event.nick)
		iproxy.logmsg(event.dtime, event.nick, user, event.target, event.msg)

def logsearch(event: Event, bot: BotLike) -> None:
	""" log [n] [searchterm]. Will search logs for searchterm. n is the number of results to display [1-99], 
	default is 6 and anything over will be output to pastebin.
	"""
	iproxy = INDEX_PROXIES.get(bot.network)
	if iproxy:
		# parse input
		if not event.argument: return bot.say(functionHelp(logsearch))
		first, remainder = argumentSplit(event.argument, 2)
		try:
			if first is None:
				raise ValueError
			parsed_limit = int(first)
			if parsed_limit > 99: raise ValueError
			elif parsed_limit < 0: raise ValueError
			limit: int | None = None if parsed_limit == 0 else parsed_limit
			query = remainder or ""
		except ValueError:
			query = event.argument
			limit = 6
		source = event.target or event.nick
		if source is None:
			return bot.say("No log source available.")
		results = iproxy.search(source, query, limit)
		if results is None:
			bot.say("Log search error happened. Check console.")
		else:
			#results.append((item["timestamp"], item["nick"], item["source"], item["content"]))
			if not results: 
				return bot.say("No results.")
			if limit is None or limit > 6:
				title = "Logsearch for (%s)" % query
				body = "%s: %%s" % title
				pastehelper(bot, body, items=[LOG_FORMAT % (x[1], x[3]) for x in results], title=title, altmsg="%s", force=True)
			else:
				bot.say("{0}", fcfs=True, strins=[LOG_FORMAT % (x[1], x[3]) for x in results], joinsep="\x02 | \x02")

def logstats(event: Event, bot: BotLike) -> None:
	iproxy = INDEX_PROXIES.get(bot.network)
	if iproxy:
		source = event.target or event.nick
		if source is None:
			return bot.say("No log source available.")
		results = iproxy.search(source, event.argument or "", None, "user")
		if not results:
			return bot.say("No results.")
		results.sort(reverse=True)
		bot.say("(%s) %s" % (event.argument, ", ".join(("%s: %s" % (nick, count) for count, nick in results))))

def _user_rename(network: str, old: str, new: str) -> None:
	iproxy = INDEX_PROXIES.get(network)
	if iproxy: iproxy.rename(old, new)
	
def init(bot: BotLike) -> bool:
	global INDEX_PROXIES
	global USERS_MODULE
	USERS_MODULE = bot.getModule("users")
	if bot.network not in INDEX_PROXIES:
		proxy = IndexProxy(bot.network, bot.getOption("indexdir", module="logindexsearch"), bot.getOption("commandprefix"))
		INDEX_PROXIES[bot.network] = proxy
		proxy.start()
		USERS_MODULE.REGISTER_UPDATE(bot.network, _user_rename, external=True)
	else:
		print("WARNING: Already have log proxy for (%s) network." % bot.network)
	return True
	
def unload() -> None:
	for lproc in INDEX_PROXIES.values():
		lproc.stop()

mappings = (Mapping(types=["privmsged"], function=logmsg), Mapping(command="log", function=logsearch),
	Mapping(command="logstats", function=logstats))
