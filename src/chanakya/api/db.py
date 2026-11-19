"""Database access for the HTTP API. The schema/migrations run ONCE per process (storage.db.connect); every request
then opens a plain connection, so concurrent requests do not re-run migrations while a background job writes."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from chanakya.storage.db import connect

ROOT = Path(__file__).resolve().parents[3]
_ready: set[str] = set()
_lock = threading.Lock()


def db_path() -> str:
    return os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data" / "chanakya.db"))


def open_db() -> sqlite3.Connection:
    path = db_path()
    with _lock:
        if path not in _ready:
            connect(path).close()  # creates new tables (additive) and runs the idempotent migrations
            _ready.add(path)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    try:  # WAL: readers never block the agents' writes (persistent per database file)
        con.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return con


@contextmanager
def db():
    con = open_db()
    try:
        yield con
    finally:
        con.close()
