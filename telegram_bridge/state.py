"""Private, locked operational state with durable at-most-once claims.

The caller supplies a path outside the source tree and stores metadata only,
never bot tokens, prompts, or agent output. Claims are committed before dispatch;
even failed or uncertain work is not automatically replayed.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading


class StateError(RuntimeError):
    """A state failure that does not disclose paths or stored data."""


class StateStore:
    def __init__(self, path: Path) -> None:
        self._connection: sqlite3.Connection | None = None
        self._lock_fd: int | None = None
        self._mutex = threading.RLock()
        try:
            path = Path(path).absolute()
            parent = path.parent
            missing = []
            cursor = parent
            while not cursor.exists():
                missing.append(cursor)
                cursor = cursor.parent
            for directory in reversed(missing):
                directory.mkdir(mode=0o700, exist_ok=True)
            if parent.is_symlink() or not parent.is_dir():
                raise StateError("State directory must be a real directory.")
            parent.chmod(0o700)
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
            lock_path = path.with_name(path.name + ".lock")
            self._lock_fd = os.open(lock_path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(self._lock_fd).st_mode):
                raise StateError("State lock must be a regular file.")
            os.fchmod(self._lock_fd, 0o600)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateError("Another Telegram bridge holds the state lock.") from None
            # Precreate securely so SQLite never opens a permissive/symlink file.
            fd = os.open(path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise StateError("State database must be a regular file.")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            self._connection = sqlite3.connect(
                str(path), timeout=5, isolation_level=None, check_same_thread=False,
            )
            self._connection.execute("PRAGMA journal_mode = DELETE")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS claimed_updates (update_id INTEGER PRIMARY KEY)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS claimed_callbacks (callback_id TEXT PRIMARY KEY)"
            )
        except StateError:
            self.close()
            raise
        except Exception:
            self.close()
            raise StateError("Cannot open private Telegram state.") from None

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StateError("Telegram state is closed.")
        return self._connection

    @staticmethod
    def _check_key(key: str) -> None:
        if not isinstance(key, str) or not key:
            raise StateError("State key must be a nonempty string.")
        if key.lower() in {"token", "bot_token", "telegram_token", "api_key", "password"}:
            raise StateError("Credentials must not be stored in Telegram state.")

    def get(self, key: str, default=None):
        self._check_key(key)
        with self._mutex:
            try:
                row = self._db().execute(
                    "SELECT value FROM metadata WHERE key = ?", (key,),
                ).fetchone()
                return default if row is None else json.loads(row[0])
            except StateError:
                raise
            except Exception:
                raise StateError("Cannot read Telegram state.") from None

    def set(self, key: str, value) -> None:
        """Atomically persist JSON metadata; the polling offset cannot decrease."""
        self._check_key(key)
        if key == "offset" and (type(value) is not int or not 0 <= value <= 2**63 - 1):
            raise StateError("State offset must be a nonnegative integer.")
        try:
            encoded = json.dumps(value, ensure_ascii=True, allow_nan=False,
                                 separators=(",", ":"))
        except Exception:
            raise StateError("State value must be JSON serializable.") from None
        with self._mutex:
            connection = self._db()
            try:
                connection.execute("BEGIN IMMEDIATE")
                if key == "offset":
                    row = connection.execute(
                        "SELECT value FROM metadata WHERE key = 'offset'",
                    ).fetchone()
                    old = 0 if row is None else json.loads(row[0])
                    if type(old) is not int or old < 0:
                        raise StateError("Stored Telegram offset is invalid.")
                    encoded = str(max(old, value))
                connection.execute(
                    "INSERT INTO metadata (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, encoded),
                )
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                raise StateError("Cannot persist Telegram state.") from None

    def claim_update(self, update_id: int) -> bool:
        """Commit a unique update claim before the caller performs any action."""
        if type(update_id) is not int or not 0 <= update_id <= 2**63 - 1:
            raise StateError("Telegram update identifier must be a nonnegative integer.")
        with self._mutex:
            connection = self._db()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO claimed_updates (update_id) VALUES (?)",
                    (update_id,),
                )
                claimed = cursor.rowcount == 1
                connection.execute("COMMIT")
                return claimed
            except Exception:
                self._rollback(connection)
                raise StateError("Cannot claim Telegram update.") from None

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        except Exception:
            pass

    def claim_callback(self, callback_id: str) -> bool:
        """Callback IDs, like updates, are claimed before any dispatch."""
        if not isinstance(callback_id, str) or not 1 <= len(callback_id) <= 256:
            raise StateError("Invalid Telegram callback identifier.")
        with self._mutex:
            try:
                cursor = self._db().execute(
                    "INSERT OR IGNORE INTO claimed_callbacks (callback_id) VALUES (?)",
                    (callback_id,),
                )
                return cursor.rowcount == 1
            except Exception:
                raise StateError("Cannot claim Telegram callback.") from None

    def close(self) -> None:
        """Release resources; safe to call repeatedly or after partial startup."""
        with self._mutex:
            connection, self._connection = self._connection, None
            fd, self._lock_fd = self._lock_fd, None
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def __enter__(self) -> StateStore:
        self._db()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
