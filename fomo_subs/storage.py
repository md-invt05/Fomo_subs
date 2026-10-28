"""Follow queue and persistent status storage."""

import csv
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path


class SingleInstance:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Another follow service is already using this profile") from exc
        return self

    def __exit__(self, *_):
        if self.file is not None:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class FollowStore:
    def __init__(self, path, recover_running=True):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS follow_jobs (
                user_id TEXT PRIMARY KEY,
                handle TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS follow_due
                ON follow_jobs(status,next_try_at);
        """)
        # A killed process never proves that its in-flight click succeeded.
        if recover_running:
            self.db.execute("UPDATE follow_jobs SET status='retry',next_try_at=0 WHERE status='running'")
            self.db.commit()
        self.snapshot_stamp = None

    def sync_snapshot(self, path, self_handle):
        path = Path(path)
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self.snapshot_stamp:
            return 0
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames != ["userId", "handle"]:
                raise ValueError("all_profiles.csv must have userId,handle columns")
            identities = {}
            for row in reader:
                user_id = row["userId"].strip()
                handle = row["handle"].strip()
                if not user_id or not handle:
                    raise ValueError("all_profiles.csv contains an empty userId or handle")
                if user_id in identities and identities[user_id] != handle:
                    raise ValueError("all_profiles.csv contains conflicting userId rows")
                identities[user_id] = handle
        collisions = Counter(handle.casefold() for handle in identities.values())
        now = time.time()
        with self.db:
            self.db.executemany("""INSERT OR IGNORE INTO follow_jobs
                (user_id,handle,status,updated_at) VALUES(?,?,'pending',?)""",
                ((user_id, handle, now) for user_id, handle in identities.items()))
            self.db.executemany("""UPDATE follow_jobs SET handle=?,
                status=CASE WHEN status='unavailable' THEN 'pending' ELSE status END,
                next_try_at=0 WHERE user_id=? AND handle<>?""",
                ((handle, user_id, handle) for user_id, handle in identities.items()))
            self.db.executemany("""UPDATE follow_jobs SET status='skipped_self',updated_at=?
                WHERE user_id=? AND status NOT IN ('followed','already_following')""",
                ((now, user_id) for user_id, handle in identities.items()
                 if handle.casefold() == self_handle.casefold()))
            self.db.executemany("""UPDATE follow_jobs SET status='ambiguous_handle',updated_at=?
                WHERE user_id=? AND status NOT IN ('followed','already_following','skipped_self')""",
                ((now, user_id) for user_id, handle in identities.items()
                 if collisions[handle.casefold()] > 1))
            self.db.executemany("""UPDATE follow_jobs SET status='pending',next_try_at=0,updated_at=?
                WHERE user_id=? AND status='ambiguous_handle'""",
                ((now, user_id) for user_id, handle in identities.items()
                 if collisions[handle.casefold()] == 1 and handle.casefold() != self_handle.casefold()))
        self.snapshot_stamp = stamp
        return len(identities)

    def claim(self):
        row = self.db.execute("""SELECT * FROM follow_jobs
            WHERE status IN ('pending','retry') AND next_try_at<=?
            ORDER BY next_try_at,user_id LIMIT 1""", (time.time(),)).fetchone()
        if row:
            self.db.execute("""UPDATE follow_jobs SET status='running',attempts=attempts+1,
                updated_at=? WHERE user_id=?""", (time.time(), row["user_id"]))
            self.db.commit()
        return row

    def mark(self, user_id, status, error=None):
        self.db.execute("""UPDATE follow_jobs SET status=?,last_error=?,updated_at=?,next_try_at=0
            WHERE user_id=?""", (status, error, time.time(), user_id))
        self.db.commit()

    def retry(self, row, error):
        attempts = row["attempts"] + 1
        delay = min(3600, 30 * 2 ** min(attempts, 7))
        self.db.execute("""UPDATE follow_jobs SET status='retry',next_try_at=?,
            updated_at=?,last_error=? WHERE user_id=?""",
            (time.time() + delay, time.time(), str(error)[:200], row["user_id"]))
        self.db.commit()

    def counts(self):
        return dict(self.db.execute("SELECT status,count(*) FROM follow_jobs GROUP BY status").fetchall())

    def close(self):
        self.db.close()
