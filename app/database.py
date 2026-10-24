"""SQLite persistence for MacroLog.

Single file database (data/macrolog.sqlite) so the whole app state can be
backed up by copying one file plus the photos directory.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

_WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject     TEXT    NOT NULL UNIQUE,   -- OIDC 'sub' claim (stable user id)
    email       TEXT,
    name        TEXT,
    picture     TEXT,
    created_at  INTEGER NOT NULL,
    last_login  INTEGER
);

CREATE TABLE IF NOT EXISTS meals (
    owner         TEXT    NOT NULL DEFAULT 'default',  -- users.subject or 'default'
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,            -- epoch seconds (local log time)
    date          TEXT    NOT NULL,            -- YYYY-MM-DD, derived from ts
    kind          TEXT    NOT NULL DEFAULT 'other',  -- breakfast|lunch|dinner|snack|drink
    description   TEXT    NOT NULL DEFAULT '',
    calories      REAL    NOT NULL DEFAULT 0,
    protein       REAL    NOT NULL DEFAULT 0,
    carbs         REAL    NOT NULL DEFAULT 0,
    fat           REAL    NOT NULL DEFAULT 0,
    confidence    REAL    NOT NULL DEFAULT 0.5,
    photo_path    TEXT,
    source        TEXT    NOT NULL DEFAULT 'photo',   -- photo|manual
    provider      TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    raw_json      TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(date);
CREATE INDEX IF NOT EXISTS idx_meals_owner ON meals(owner, date);

CREATE TABLE IF NOT EXISTS weights (
    owner   TEXT    NOT NULL DEFAULT 'default',
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    date    TEXT    NOT NULL,
    value   REAL    NOT NULL,
    source  TEXT    NOT NULL DEFAULT 'manual'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weights_date ON weights(owner, date);

CREATE TABLE IF NOT EXISTS body_fat (
    owner  TEXT    NOT NULL DEFAULT 'default',
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    date   TEXT    NOT NULL,
    value  REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bf_date ON body_fat(owner, date);

CREATE TABLE IF NOT EXISTS exercises (
    owner         TEXT    NOT NULL DEFAULT 'default',
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    date        TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    calories    REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ex_date ON exercises(owner, date);

CREATE TABLE IF NOT EXISTS estimates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id      INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    field        TEXT    NOT NULL,   -- calories|protein|carbs|fat
    predicted    REAL    NOT NULL,
    actual       REAL    NOT NULL,
    abs_error    REAL    NOT NULL,
    rel_error    REAL    NOT NULL,
    logged_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_est_field ON estimates(field);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with _WRITE_LOCK:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        """Idempotent upgrades for databases created before per-user scoping.

        CREATE TABLE IF NOT EXISTS won't add columns to existing tables, so we
        detect and ALTER. Safe to run every startup.
        """
        has_owner = {}
        for tbl in ("meals", "weights", "body_fat", "exercises"):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({tbl})")}
            has_owner[tbl] = "owner" in cols
            if not has_owner[tbl]:
                self._conn.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN owner TEXT NOT NULL DEFAULT 'default'")
        # Recreate the two unique indexes that must now include owner.
        if not has_owner["weights"]:
            self._conn.execute("DROP INDEX IF EXISTS idx_weights_date")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_weights_date ON weights(owner, date)")
        if not has_owner["body_fat"]:
            self._conn.execute("DROP INDEX IF EXISTS idx_bf_date")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_bf_date ON body_fat(owner, date)")
        if not has_owner["meals"]:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_meals_owner ON meals(owner, date)")
        if not has_owner["exercises"]:
            self._conn.execute("DROP INDEX IF EXISTS idx_ex_date")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ex_date ON exercises(owner, date)")

    # -- users -----------------------------------------------------------
    def upsert_user(self, subject: str, email=None, name=None, picture=None) -> dict:
        now = int(time.time())
        self.execute(
            "INSERT INTO users(subject,email,name,picture,created_at,last_login) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(subject) DO UPDATE SET "
            "email=COALESCE(excluded.email,email), name=COALESCE(excluded.name,name), "
            "picture=COALESCE(excluded.picture,picture), last_login=excluded.last_login",
            (subject, email, name, picture, now, now))
        row = self.one("SELECT * FROM users WHERE subject=?", (subject,))
        return dict(row) if row else {"subject": subject}

    def get_user(self, subject: str):
        row = self.one("SELECT * FROM users WHERE subject=?", (subject,))
        return dict(row) if row else None

    # -- helpers ---------------------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()):
        return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()):
        with _WRITE_LOCK:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    def executemany(self, sql: str, seq):
        with _WRITE_LOCK:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    # -- settings --------------------------------------------------------
    def get_setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        try:
            import json as _json
            return _json.loads(row["value"])
        except Exception:
            return row["value"]

    def set_setting(self, key: str, value) -> None:
        import json as _json
        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _json.dumps(value)),
        )

    # -- meals -----------------------------------------------------------
    def add_meal(self, *, date: str, ts: int, kind: str, description: str,
                 calories: float, protein: float, carbs: float, fat: float,
                 confidence: float, photo_path, source: str,
                 provider: str, model: str, raw_json: str,
                 owner: str = "default") -> int:
        return self.execute(
            "INSERT INTO meals(owner,ts,date,kind,description,calories,protein,carbs,fat,"
            "confidence,photo_path,source,provider,model,raw_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, ts, date, kind, description, calories, protein, carbs, fat,
             confidence, photo_path, source, provider, model, raw_json,
             int(time.time())),
        )

    def update_meal(self, meal_id: int, owner: str = "default", **fields) -> None:
        allowed = {"kind", "description", "calories", "protein", "carbs", "fat"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ",".join(f"{k}=?" for k in sets)
        self.execute(f"UPDATE meals SET {cols} WHERE id=? AND owner=?",
                     (*sets.values(), meal_id, owner))

    def get_meal(self, meal_id: int, owner: str = "default"):
        return self.one("SELECT * FROM meals WHERE id=? AND owner=?", (meal_id, owner))

    def delete_meal(self, meal_id: int, owner: str = "default") -> sqlite3.Row | None:
        row = self.get_meal(meal_id, owner)
        if row:
            self.execute("DELETE FROM estimates WHERE meal_id=?", (meal_id,))
            self.execute("DELETE FROM meals WHERE id=? AND owner=?", (meal_id, owner))
        return row

    def meals_for_date(self, date: str, owner: str = "default") -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM meals WHERE date=? AND owner=? ORDER BY ts", (date, owner))

    # -- weights ---------------------------------------------------------
    def set_weight(self, date: str, ts: int, value: float, owner: str = "default") -> None:
        self.execute(
            "INSERT INTO weights(owner,ts,date,value) VALUES(?,?,?,?) "
            "ON CONFLICT(owner,date) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (owner, ts, date, value))

    def set_body_fat(self, date: str, ts: int, value: float, owner: str = "default") -> None:
        self.execute(
            "INSERT INTO body_fat(owner,ts,date,value) VALUES(?,?,?,?) "
            "ON CONFLICT(owner,date) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (owner, ts, date, value))

    def close(self):
        self._conn.close()
