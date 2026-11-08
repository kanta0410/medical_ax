"""SQLite。接続は呼び出しごとに開き、充足確定は IMMEDIATE トランザクションで行う。"""

from __future__ import annotations

from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  leader INTEGER NOT NULL,
  night_ok INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS preference (
  staff_id TEXT NOT NULL,
  day INTEGER NOT NULL,
  PRIMARY KEY (staff_id, day)
);
CREATE TABLE IF NOT EXISTS baseline (
  staff_id TEXT NOT NULL,
  day INTEGER NOT NULL,
  shift TEXT NOT NULL,
  PRIMARY KEY (staff_id, day)
);
CREATE TABLE IF NOT EXISTS assignment (
  staff_id TEXT NOT NULL,
  day INTEGER NOT NULL,
  shift TEXT NOT NULL,
  PRIMARY KEY (staff_id, day)
);
CREATE TABLE IF NOT EXISTS vacancy (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_id TEXT NOT NULL,
  day INTEGER NOT NULL,
  shift TEXT NOT NULL,
  status TEXT NOT NULL,
  solver_code TEXT NOT NULL,
  filled_staff_id TEXT,
  filled_external_name TEXT,
  filled_external_leader INTEGER NOT NULL DEFAULT 0,
  filled_external_night_ok INTEGER NOT NULL DEFAULT 0,
  fill_route TEXT,
  commit_code TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offer (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vacancy_id INTEGER NOT NULL,
  staff_id TEXT NOT NULL,
  eligible INTEGER NOT NULL,
  change_count INTEGER,
  preference_violations INTEGER,
  solver_code TEXT NOT NULL,
  reason TEXT NOT NULL,
  response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posting (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vacancy_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL,
  admin_confirmed INTEGER NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS application (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  posting_id INTEGER NOT NULL,
  applicant_name TEXT NOT NULL,
  night_ok INTEGER NOT NULL,
  leader INTEGER NOT NULL,
  accepted INTEGER NOT NULL,
  reason TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
