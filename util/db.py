from collections.abc import Callable, Sequence
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from queue import Queue
import sqlite3
from threading import Lock, Thread
from traceback import format_exc
from typing import Any

from .types import DatabaseParams

log = getLogger(__name__)


Query = tuple[str, DatabaseParams]
fetchone = sqlite3.Cursor.fetchone
fetchall = sqlite3.Cursor.fetchall
fetchmany = sqlite3.Cursor.fetchmany

_STOP = object()


@dataclass(slots=True)
class _Result:
    value: Any = None
    error: Exception | None = None
    worker_traceback: str | None = None

    def unwrap(self) -> Any:
        if self.error is not None:
            if self.worker_traceback:
                self.error.add_note(
                    "Database worker traceback:\n" + self.worker_traceback
                )
            raise self.error
        return self.value


class DBManager:
    """Own database workers and map IRC networks onto database files."""

    def __init__(self, datadir: str, datafile: str) -> None:
        self.serverDBMap: dict[str, DBaccess] = {}
        self.fileDBMap: dict[str, DBaccess] = {}
        self.mainDB = DBaccess(datadir, datafile)
        self.datadir = datadir
        self.datafile = datafile
        self.managerThread = ManagerThread()
        self.managerThread.start()
        self.mainDB.start()
        self.running = True

    def query(
        self,
        serverlabel: str,
        query: str,
        params: DatabaseParams = (),
        func: Callable[[sqlite3.Cursor], Any] | None = None,
    ) -> Any:
        database = self.managerThread.call(self._getDB, serverlabel)
        return database.query(query, params, func)

    def batch(
        self, serverlabel: str, queries: Sequence[Query]
    ) -> list[list[sqlite3.Row]]:
        database = self.managerThread.call(self._getDB, serverlabel)
        return database.batch(queries)

    def _addServer(self, serverlabel: str, datafile: str) -> None:
        if datafile == self.datafile:
            old_database = self.serverDBMap.pop(serverlabel, None)
            if old_database is not None:
                self._release(old_database)
            return
        old_database = self.serverDBMap.get(serverlabel)
        if old_database is not None and old_database.datafile == datafile:
            return
        if old_database is not None:
            self._release(old_database)
        database = self.fileDBMap.get(datafile)
        if database is None:
            database = DBaccess(self.datadir, datafile)
            self.fileDBMap[datafile] = database
            database.start()
        else:
            database.servers += 1
        self.serverDBMap[serverlabel] = database

    def _release(self, database: "DBaccess") -> None:
        database.servers -= 1
        if database.servers == 0:
            database.stop()
            self.fileDBMap.pop(database.datafile, None)

    def addServer(self, serverlabel: str, datafile: str) -> None:
        self.managerThread.call(self._addServer, serverlabel, datafile)

    def _delServer(self, serverlabel: str) -> None:
        database = self.serverDBMap.pop(serverlabel, None)
        if database is not None:
            self._release(database)

    def delServer(self, serverlabel: str) -> None:
        self.managerThread.call(self._delServer, serverlabel)

    def _getDB(self, serverlabel: str) -> "DBaccess":
        return self.serverDBMap.get(serverlabel, self.mainDB)

    def _shutdown(self) -> None:
        for database in set(self.serverDBMap.values()):
            database.stop()
        self.serverDBMap.clear()
        self.fileDBMap.clear()
        self.mainDB.stop()

    def shutdown(self) -> None:
        if self.running:
            self.managerThread.call(self._shutdown)
            self.managerThread.stop()
            self.running = False

    def dbCheckCreateTable(
        self, serverlabel: str, tablename: str, createstmt: str
    ) -> bool:
        if not self.query(
            serverlabel,
            "SELECT name FROM sqlite_master WHERE name=?;",
            (tablename,),
        ):
            self.query(serverlabel, createstmt)
        return True

    def _dbcommit(self) -> None:
        for database in set(self.serverDBMap.values()):
            database.checkpoint()
        self.mainDB.checkpoint()

    def dbcommit(self) -> None:
        self.managerThread.call(self._dbcommit)


class ManagerThread(Thread):
    def __init__(self) -> None:
        super().__init__(name="ManagerThread")
        self.callQueue: Queue[Any] = Queue()

    def run(self) -> None:
        while True:
            call = self.callQueue.get()
            if call is _STOP:
                return
            result_queue, function, args, kwargs = call
            try:
                result = _Result(value=function(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001 - manager thread boundary
                result = _Result(error=exc, worker_traceback=format_exc())
            result_queue.put(result)

    def call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if not self.is_alive():
            raise RuntimeError("Database manager thread is not running.")
        result_queue: Queue[_Result] = Queue(maxsize=1)
        self.callQueue.put((result_queue, function, args, kwargs))
        return result_queue.get().unwrap()

    def stop(self) -> None:
        if self.is_alive():
            self.callQueue.put(_STOP)
            self.join()


class DBaccess(Thread):
    """Serialize access to one SQLite connection in a dedicated thread."""

    def __init__(self, datadir: str, datafile: str) -> None:
        if Path(datafile).is_absolute():
            raise ValueError("Database file must be relative to datadir.")
        data_directory = Path(datadir).resolve()
        data_directory.mkdir(parents=True, exist_ok=True)
        if not data_directory.is_dir():
            raise OSError("datadir must be a directory")
        database_path = (data_directory / datafile).resolve()
        if not database_path.is_relative_to(data_directory):
            raise ValueError("Database file escapes datadir.")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        database_existed = database_path.exists()

        super().__init__(name="DBaccessThread(%s)" % datafile)
        self.datafile = datafile
        self.f = str(database_path)
        self.qq: Queue[Any] = Queue()
        self.servers = 1
        self._stopping = False
        self._submit_lock = Lock()
        if not database_existed:
            # create the file so it can be locked down before any data is written
            sqlite3.connect(self.f, timeout=10).close()
            database_path.chmod(0o600)

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")

    def run(self) -> None:
        connection = sqlite3.connect(self.f, timeout=10, isolation_level=None)
        self._configure(connection)
        try:
            while True:
                work = self.qq.get()
                if work is _STOP:
                    return
                kind, payload, result_queue = work
                try:
                    if kind == "batch":
                        result = self._execute_batch(connection, payload)
                    elif kind == "checkpoint":
                        result = connection.execute(
                            "PRAGMA wal_checkpoint(PASSIVE)"
                        ).fetchall()
                    else:
                        query, params, function = payload
                        cursor = connection.execute(query, params)
                        result = function(cursor) if function else cursor.fetchall()
                    result_queue.put(_Result(value=result))
                except Exception as exc:  # noqa: BLE001 - database thread boundary
                    result_queue.put(_Result(error=exc, worker_traceback=format_exc()))
        finally:
            connection.close()

    @staticmethod
    def _execute_batch(
        connection: sqlite3.Connection, queries: Sequence[Query]
    ) -> list[list[sqlite3.Row]]:
        results: list[list[sqlite3.Row]] = []
        connection.execute("BEGIN IMMEDIATE")
        try:
            for query, params in queries:
                results.append(connection.execute(query, params).fetchall())
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return results

    def _submit(self, work: Any) -> None:
        # the lock makes the running-check and enqueue atomic with stop(), so
        # work can never land behind _STOP (where it would block its caller
        # forever waiting on a result the worker will never produce)
        with self._submit_lock:
            if not self.is_alive() or self._stopping:
                raise RuntimeError("Attempted query on non-running %s" % self.name)
            self.qq.put(work)

    def query(
        self,
        query: str,
        params: DatabaseParams = (),
        func: Callable[[sqlite3.Cursor], Any] | None = None,
    ) -> Any:
        result_queue: Queue[_Result] = Queue(maxsize=1)
        self._submit(("query", (query, params, func), result_queue))
        return result_queue.get().unwrap()

    def batch(self, queries: Sequence[Query]) -> list[list[sqlite3.Row]]:
        result_queue: Queue[_Result] = Queue(maxsize=1)
        self._submit(("batch", tuple(queries), result_queue))
        return result_queue.get().unwrap()

    def checkpoint(self) -> None:
        result_queue: Queue[_Result] = Queue(maxsize=1)
        self._submit(("checkpoint", None, result_queue))
        result_queue.get().unwrap()

    def stop(self) -> None:
        with self._submit_lock:
            if not self.is_alive() or self._stopping:
                return
            self._stopping = True
            self.qq.put(_STOP)
        log.info("stopping %s", self.name)
        self.join()

    def commit(self) -> None:
        """Compatibility alias: autocommit is active, so checkpoint the WAL."""
        self.checkpoint()
