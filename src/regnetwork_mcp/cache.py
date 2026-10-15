"""Small, bounded local result cache; never a mirror of the whole database."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import threading
from typing import Any, Callable


@dataclass(frozen=True)
class CacheHit:
    payload: Any
    created_at: float
    stale: bool


class CacheError(RuntimeError):
    code = 'LOCAL_CACHE_ERROR'


class ResultCache:
    def __init__(self, path: Path, *, ttl: float = 86400, retention: float = 604800,
                 max_bytes: int = 50 * 1024 * 1024, clock: Callable[[], float] = time.time):
        if ttl <= 0 or retention < ttl or max_bytes <= 0:
            raise ValueError("invalid cache bounds")
        self.path, self.ttl, self.retention, self.max_bytes, self.clock = path, ttl, retention, max_bytes, clock
        self._local = threading.local()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, namespace TEXT NOT NULL, payload TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL, touched REAL NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS entries_age ON entries(created)")
            connection.execute("CREATE INDEX IF NOT EXISTS entries_touch ON entries(touched,created,key)")

    @contextmanager
    def _connection(self):
        active = getattr(self._local, 'connection', None)
        if active is not None:
            yield active
            return
        try:
            connection = sqlite3.connect(self.path, timeout=5)
        except sqlite3.Error as exc:
            raise CacheError('could not open local result cache') from exc
        try:
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise CacheError('local result cache could not be read or written') from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def batch(self):
        """Use one transaction for related records; prune once before committing."""
        if getattr(self._local, 'connection', None) is not None:
            yield
            return
        with self._connection() as connection:
            self._local.connection = connection
            try:
                yield
                self._prune(connection)
            finally:
                self._local.connection = None

    @staticmethod
    def key(namespace: str, parameters: Any) -> str:
        encoded = json.dumps([namespace, parameters], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _prune(self, connection):
        connection.execute("DELETE FROM entries WHERE created < ?", (self.clock() - self.retention,))
        total = connection.execute("SELECT COALESCE(SUM(size),0) FROM entries").fetchone()[0]
        if total > self.max_bytes:
            for key, size in connection.execute("SELECT key,size FROM entries ORDER BY touched,created,key").fetchall():
                connection.execute("DELETE FROM entries WHERE key=?", (key,))
                total -= size
                if total <= self.max_bytes:
                    break

    def put(self, key: str, payload: Any, *, namespace: str = "query") -> bool:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        size = len(encoded.encode())
        if size > self.max_bytes:
            return False
        now = self.clock()
        with self._connection() as connection:
            connection.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?)", (key, namespace, encoded, size, now, now))
            if getattr(self._local, 'connection', None) is None:
                self._prune(connection)
        return True

    def get(self, key: str, *, allow_expired: bool = False) -> CacheHit | None:
        with self._connection() as connection:
            # Expired retention is filtered on lookup; reads need no whole-cache scan.
            row = connection.execute("SELECT payload,created FROM entries WHERE key=? AND created>=?",
                                     (key, self.clock()-self.retention)).fetchone()
            if row is None:
                return None
            payload, created = row
            stale = self.clock() - created >= self.ttl
            if stale and not allow_expired:
                return None
            connection.execute("UPDATE entries SET touched=? WHERE key=?", (self.clock(), key))
            try:
                decoded = json.loads(payload)
            except (ValueError, TypeError) as exc:
                raise CacheError('local result cache contains invalid JSON') from exc
            return CacheHit(decoded, created, stale)

    def statistics(self) -> dict[str, Any]:
        with self._connection() as connection:
            self._prune(connection)
            count, size = connection.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM entries").fetchone()
        return {"entries": count, "payload_bytes": size, "payload_budget_bytes": self.max_bytes,
                "ttl_seconds": self.ttl, "retention_seconds": self.retention,
                "database_file": str(self.path), "whole_database_mirror": False}
