"""SQLite TTL cache for world-intel-mcp.

Simple key-value cache with per-key TTL, WAL mode, and automatic eviction.
No external deps — just stdlib sqlite3.
"""

import json
import logging
import os
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger("world-intel-mcp.cache")

_DEFAULT_DB = Path.home() / ".cache" / "world-intel-mcp" / "cache.db"


class CachePathError(PermissionError):
    """Unsafe cache storage must not be opened or silently replaced."""


def _check_directory(info: os.stat_result, path: Path, *, sensitive: bool) -> None:
    uid = os.geteuid()
    if not stat.S_ISDIR(info.st_mode):
        raise CachePathError(f"Unsafe cache directory (not a directory): {path}")
    if info.st_uid not in ({uid} if sensitive else {0, uid}):
        raise CachePathError(f"Unsafe cache directory ownership: {path}")
    if info.st_mode & 0o022:
        # A sticky ancestor such as /tmp can safely contain our owned child.
        # The sensitive directory itself must never be shared-writable.
        if sensitive or not info.st_mode & stat.S_ISVTX:
            raise CachePathError(f"Unsafe writable cache directory: {path}")


def _check_file(info: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise CachePathError(f"Unsafe cache file (not a regular file): {path}")
    if info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise CachePathError(f"Unsafe cache file ownership or hard links: {path}")
    if info.st_mode & 0o022:
        raise CachePathError(f"Unsafe writable cache file: {path}")


@contextmanager
def _secure_cache_file(path: Path, *, private_dir: bool):
    """Prepare POSIX storage without following untrusted symlinks or chmodding them.

    Walk directories through pinned descriptors. SQLite must still open by
    pathname for its sidecars: this does not defend against concurrent path
    replacement by this UID/root, or filesystems ignoring POSIX permissions.
    """
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise CachePathError("Secure cache storage requires POSIX O_NOFOLLOW support")
    absolute = path.absolute()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(absolute.anchor, directory_flags)
    file_fd = None
    try:
        _check_directory(os.fstat(directory_fd), Path(absolute.anchor), sensitive=False)
        current = Path(absolute.anchor)
        for part in absolute.parent.parts[1:]:
            current /= part
            try:
                info = os.stat(part, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass  # another process created it; verify rather than trust
                info = os.stat(part, dir_fd=directory_fd, follow_symlinks=False)
            sensitive = current == absolute.parent
            _check_directory(info, current, sensitive=sensitive)
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                _check_directory(opened, current, sensitive=sensitive)
                if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                    raise CachePathError(f"Cache directory changed during verification: {current}")
            except BaseException:
                os.close(child_fd)
                raise
            os.close(directory_fd)
            directory_fd = child_fd
        # Also covers a database placed directly under the filesystem root.
        _check_directory(os.fstat(directory_fd), absolute.parent, sensitive=True)
        if private_dir:
            os.fchmod(directory_fd, 0o700)

        for name in (absolute.name, absolute.name + "-wal", absolute.name + "-shm",
                     absolute.name + "-journal"):
            is_database = name == absolute.name
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not is_database:
                    continue
                try:
                    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory_fd)
                except FileExistsError:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                else:
                    os.close(fd)
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            _check_file(info, absolute.parent / name)
            try:
                fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except FileNotFoundError:
                if not is_database:
                    continue  # SQLite may remove sidecars on another connection's close
                raise
            try:
                opened = os.fstat(fd)
                if (
                    not is_database
                    and opened.st_nlink == 0
                    and stat.S_ISREG(opened.st_mode)
                    and opened.st_uid == os.geteuid()
                ):
                    # The last SQLite connection may unlink a sidecar after
                    # our open. It is no longer reachable; do not chmod it.
                    continue
                _check_file(opened, absolute.parent / name)
                if is_database and (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                    raise CachePathError(f"Cache file changed during verification: {absolute.parent / name}")
                # Sidecars may have been recreated between stat/open by a
                # concurrent connection. Their opened inode must independently
                # pass every safety check above, but need not be the old inode.
                os.fchmod(fd, 0o600)
                if is_database:
                    file_fd, fd = fd, None
            finally:
                if fd is not None:
                    os.close(fd)
        yield absolute
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _default_db_path() -> Path:
    if env_path := os.environ.get("WORLD_INTEL_CACHE_DB"):
        return Path(env_path).expanduser()
    if xdg_cache := os.environ.get("XDG_CACHE_HOME"):
        return Path(xdg_cache).expanduser() / "world-intel-mcp" / "cache.db"
    return _DEFAULT_DB


class Cache:
    """SQLite-backed TTL cache."""

    def __init__(self, db_path: Path | None = None):
        explicit_path = db_path is not None or bool(
            os.environ.get("WORLD_INTEL_CACHE_DB") or os.environ.get("XDG_CACHE_HOME")
        )
        self.db_path = (Path(db_path) if db_path is not None else _default_db_path()).absolute()
        self._private_dir = db_path is None and not os.environ.get("WORLD_INTEL_CACHE_DB")
        self._conn: sqlite3.Connection | None = None
        try:
            self._init_db()
        except CachePathError:
            self.close()
            raise
        except (OSError, sqlite3.OperationalError) as exc:
            self.close()
            if explicit_path:
                raise
            # Canonicalize only the system-selected temp root (e.g. /tmp is a
            # root-managed alias on macOS), never the untrusted cache leaf.
            fallback = Path(tempfile.gettempdir()).resolve() / f"world-intel-mcp-{os.geteuid()}" / "cache.db"
            logger.warning(
                "Cache unavailable at %s: %s; falling back to %s",
                self.db_path,
                exc,
                fallback,
            )
            self.db_path = fallback
            self._private_dir = True
            try:
                self._init_db()
            except BaseException:
                self.close()
                raise
        except BaseException:
            self.close()
            raise

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)"
        )
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            with _secure_cache_file(self.db_path, private_dir=self._private_dir) as path:
                # mode=rw prevents SQLite recreating a missing main file with
                # umask-dependent permissions after our exclusive 0600 creation.
                conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)
                try:
                    conn.execute("PRAGMA busy_timeout=5000")
                    # Concurrent first opens can both attempt the WAL mode
                    # transition. SQLite may return BUSY immediately for that
                    # PRAGMA despite busy_timeout; retry only lock contention.
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            conn.execute("PRAGMA journal_mode=WAL")
                            break
                        except sqlite3.OperationalError as exc:
                            code = getattr(exc, "sqlite_errorcode", 0) & 0xff
                            if (
                                code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                                or time.monotonic() >= deadline
                            ):
                                raise
                            time.sleep(0.01)
                    conn.execute("PRAGMA synchronous=NORMAL")
                except BaseException:
                    conn.close()
                    raise
                self._conn = conn
        return self._conn

    def get(self, key: str) -> Any | None:
        """Get a cached value. Returns None if missing or expired."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        value, expires_at = row
        if time.time() > expires_at:
            return None
        return json.loads(value)

    def get_stale(self, key: str) -> Any | None:
        """Get cached value even if expired (last-known-good fallback)."""
        meta = self.get_stale_meta(key)
        return meta[0] if meta is not None else None

    def get_stale_meta(self, key: str) -> tuple[Any, float] | None:
        """Get cached value even if expired, plus when it was originally written.

        Returns ``(value, created_at)`` (``created_at`` is a Unix timestamp)
        or ``None`` if the key doesn't exist at all. Callers that need to
        report how old a stale fallback is — or preserve the original
        fetch time instead of restamping "now" — use this instead of
        ``get_stale()``.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value, created_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        value, created_at = row
        return json.loads(value), created_at

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """Store a value with TTL in seconds.

        Write failures (readonly DB, locked) are logged and swallowed —
        the cache is best-effort and must never crash the caller.
        """
        try:
            conn = self._get_conn()
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (key, json.dumps(value, default=str), now + ttl_seconds, now),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning("Cache write failed for %s: %s", key, exc)

    def delete(self, key: str) -> None:
        """Delete a specific key."""
        conn = self._get_conn()
        conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        conn.commit()

    def evict_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "DELETE FROM cache WHERE expires_at < ?", (time.time(),)
            )
            conn.commit()
            return cursor.rowcount
        except sqlite3.OperationalError as exc:
            logger.warning("Cache evict failed: %s", exc)
            return 0

    def stats(self) -> dict[str, Any]:
        """Cache statistics."""
        conn = self._get_conn()
        now = time.time()
        total = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        expired = conn.execute(
            "SELECT COUNT(*) FROM cache WHERE expires_at < ?", (now,)
        ).fetchone()[0]
        return {
            "total_entries": total,
            "active_entries": total - expired,
            "expired_entries": expired,
            "db_path": str(self.db_path),
        }

    def freshness(self) -> dict[str, Any]:
        """Per-source freshness report — shows age of newest entry per source prefix."""
        conn = self._get_conn()
        now = time.time()
        rows = conn.execute(
            "SELECT key, created_at, expires_at FROM cache ORDER BY created_at DESC"
        ).fetchall()
        sources: dict[str, dict] = {}
        for key, created_at, expires_at in rows:
            # Extract source prefix (e.g., "markets:quotes:^GSPC" → "markets")
            prefix = key.split(":")[0] if ":" in key else key
            if prefix not in sources:
                age_s = now - created_at
                is_stale = now > expires_at
                sources[prefix] = {
                    "last_updated_s_ago": round(age_s, 1),
                    "is_stale": is_stale,
                    "newest_key": key,
                }
        return sources

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
