from threading import Thread
from queue import Queue
from traceback import print_exc

from os.path import exists, join, isfile
from os import mkdir
import sqlite3

fetchone = sqlite3.Cursor.fetchone
fetchall = sqlite3.Cursor.fetchall
fetchmany = sqlite3.Cursor.fetchmany

### DBManager
### DBManager managers the global bot database and server-specific databases.
### To use a specific database for a server you must configure that server to have a unique datafile.

class DBManager:
	def __init__(self, datadir, datafile):
		self.serverDBMap = {}
		self.fileDBMap = {}
		self.mainDB = DBaccess(datadir, datafile)
		self.datadir = datadir
		self.datafile = datafile
		self.managerThread = ManagerThread()
		self.managerThread.start()
		self.mainDB.start()
		self.running = True
		
	def query(self, serverlabel, q, params=(), func=None):
		db = self.managerThread.call(self._getDB, serverlabel)
		return db.query(q, params, func)
	
	def batch(self, serverlabel, qs):
		db = self.managerThread.call(self._getDB, serverlabel)
		return db.batch(qs)
		
	def _addServer(self, serverlabel, datafile):
		if not datafile == self.datafile:
			if serverlabel in self.serverDBMap:
				#determine if we need to shutdown DB and restart with different file
				if datafile == self.serverDBMap[serverlabel].datafile:
					return # exists and is correct file
				# stop
				self.serverDBMap[serverlabel].stop()
			
			# add new server if datafile isn't already used
			if datafile in self.fileDBMap:
				db = self.fileDBMap[datafile]
				db.servers += 1
				self.serverDBMap[serverlabel] = db
			else:
				db = DBaccess(self.datadir, datafile)
				self.serverDBMap[serverlabel] = db
				self.fileDBMap[datafile] = db
				db.start()
	
	def addServer(self, serverlabel, datafile):
		self.managerThread.call(self._addServer, serverlabel, datafile)
		
	def _delServer(self, serverlabel):
		if serverlabel in self.serverDBMap:
			db = self.serverDBMap[serverlabel]
			if db.datafile != self.datafile:
				db.servers -= 1
				if db.servers < 1:
					db.stop()
					del self.fileDBMap[db.datafile]
				del self.serverDBMap[serverlabel]
	
	def delServer(self, serverlabel):
		self.managerThread.call(self._delServer, serverlabel)
	
	def _getDB(self, serverlabel):
		return self.serverDBMap.get(serverlabel, self.mainDB)
		
	def _shutdown(self):
		for db in self.serverDBMap.values():
			db.stop()
		self.mainDB.stop()
	
	def shutdown(self):
		# TODO: probably lock on this so that if you CTRL+C while updaterelaunching 
		# 	you won't run in to race condition if CTRL+C while shutting down threads
		if self.running:
			self.managerThread.call(self._shutdown)
			self.managerThread.stop()
			self.running = False # to make it easier to shutdown from multiple pathways
		
	#DB helper for easy module use:
	def dbCheckCreateTable(self, serverlabel, tablename, createstmt):
		if not self.query(serverlabel, '''SELECT name FROM sqlite_master WHERE name=?;''', (tablename,)):
			self.query(serverlabel, createstmt)
		return True
		
	def _dbcommit(self):
		for db in self.serverDBMap.values():
			db.commit()
		self.mainDB.commit()
		
	def dbcommit(self):
		self.managerThread.call(self._dbcommit)
			
	
class ManagerThread(Thread):
	def __init__(self):
		super().__init__()
		self.callQueue = Queue()
		self.name = "ManagerThread"
	
	def run(self):
		while True:
			c = self.callQueue.get()
			if c == "QUIT":
				break
			q, f, args, kwargs = c
			try: ret = f(*args, **kwargs)
			except Exception as e:
				ret = e
			q.put(ret)
			
	def call(self, f, *args, **kwargs):
		q = Queue()
		self.callQueue.put((q, f, args, kwargs))
		ret = q.get()
		if isinstance(ret, Exception):
			raise ret
		return ret
		
	def stop(self):
		self.callQueue.put("QUIT")
		self.join()


# how should we deal with commits and stuff, can you even commit with execute? 
# You can if you change transactional mode. What transactional mode do we want?
class DBaccess(Thread):
	def __init__(self, datadir, datafile):
		super().__init__()
		self.name = "DBaccessThread(%s)" % datafile
		self.datafile = datafile
		if not exists(datadir):
			mkdir(datadir)
		elif isfile(datadir):
			raise OSError("datadir should not be file")
		self.f = join(datadir, self.datafile)
		self.qq = Queue() # QueryQueue, QQ
		self.servers = 1
		#just to see if we can open the file/db
		dbcon = sqlite3.connect(self.f)
		dbcon.close()
		
	def run(self):
		dbcon = sqlite3.connect(self.f)
		dbcon.row_factory = sqlite3.Row
		
		while True:
			query = self.qq.get()
			try:
				if query == "STOP":
					break
				# TODO: Test commit methods
				elif query == "COMMIT":
					dbcon.commit()
					continue
				# special batch mode
				if len(query) == 2:
					qs, resultq = query
					for q, params in qs:
						try: resultq.put(dbcon.execute(q, params).fetchall())
						except Exception as e: resultq.put(e)
				else:
					#func should be something that can work with a cursor object
					# e.g. sqlite3.Cursor.fetchall
					query, params, func, resultq = query
					if func:
						resultq.put(func(dbcon.execute(query, params)))
					else:
						resultq.put(dbcon.execute(query, params).fetchall())
			except Exception as e:
				# this is maximum cheating since variables have function scope even if defined in subblock
				if resultq: resultq.put(e)
				else: print_exc()
		dbcon.commit()
		dbcon.close()
		
	def query(self, q, params=(), func=None):
		if not self.is_alive():
			raise RuntimeError("Attempted query on non running (%s)" % self.name)
		resultq = Queue()
		self.qq.put((q, params, func, resultq))
		result = resultq.get()
		if isinstance(result, Exception):
			raise result
		return result
	
	def batch(self, qs):
		if not self.is_alive():
			raise RuntimeError("Attempted query on non running (%s)" % self.name)
		resultq = Queue()
		self.qq.put((qs, resultq))
		results = []
		# get all results. probably not needed.
		for i in range(len(qs)):
			result = resultq.get()
			if isinstance(result, Exception):
				print("Exception with: %s" % str(qs[i]))
				raise result
				# TODO: how to handle multiple exceptions?
			results.append(result)
		return results
		
	def stop(self):
		self.qq.put("STOP")
		print("STOPPING %s" % self.name)
		self.join()
		
	def commit(self):
		self.qq.put("COMMIT")
