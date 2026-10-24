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

CREATE TABLE IF NOT EXISTS meals (
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

CREATE TABLE IF NOT EXISTS weights (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    date    TEXT    NOT NULL,
    value   REAL    NOT NULL,
    source  TEXT    NOT NULL DEFAULT 'manual'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weights_date ON weights(date);

CREATE TABLE IF NOT EXISTS body_fat (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    date   TEXT    NOT NULL,
    value  REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bf_date ON body_fat(date);

CREATE TABLE IF NOT EXISTS exercises (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    date        TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    calories    REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ex_date ON exercises(date);

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
            self._conn.commit()

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
                 provider: str, model: str, raw_json: str) -> int:
        return self.execute(
            "INSERT INTO meals(ts,date,kind,description,calories,protein,carbs,fat,"
            "confidence,photo_path,source,provider,model,raw_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, date, kind, description, calories, protein, carbs, fat,
             confidence, photo_path, source, provider, model, raw_json,
             int(time.time())),
        )

    def update_meal(self, meal_id: int, **fields) -> None:
        allowed = {"kind", "description", "calories", "protein", "carbs", "fat"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ",".join(f"{k}=?" for k in sets)
        self.execute(f"UPDATE meals SET {cols} WHERE id=?",
                     (*sets.values(), meal_id))

    def delete_meal(self, meal_id: int) -> sqlite3.Row | None:
        row = self.one("SELECT * FROM meals WHERE id=?", (meal_id,))
        if row:
            self.execute("DELETE FROM estimates WHERE meal_id=?", (meal_id,))
            self.execute("DELETE FROM meals WHERE id=?", (meal_id,))
        return row

    def meals_for_date(self, date: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM meals WHERE date=? ORDER BY ts", (date,))

    # -- weights ---------------------------------------------------------
    def set_weight(self, date: str, ts: int, value: float) -> None:
        self.execute(
            "INSERT INTO weights(ts,date,value) VALUES(?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (ts, date, value))

    def set_body_fat(self, date: str, ts: int, value: float) -> None:
        self.execute(
            "INSERT INTO body_fat(ts,date,value) VALUES(?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (ts, date, value))

    def close(self):
        self._conn.close()
