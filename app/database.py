"""SQLite persistence for MacroLog.

Single file database (data/macrolog.sqlite) so the whole app state can be
backed up by copying one file plus the photos directory.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time

# Serializes writers in this process (SQLite allows one writer at a time anyway).
# Re-entrant so a transaction() block can call execute() inside it.
_WRITE_LOCK = threading.RLock()

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
    fiber         REAL    NOT NULL DEFAULT 0,   -- subset OF carbs, not extra
    confidence    REAL    NOT NULL DEFAULT 0.5,
    photo_path    TEXT,
    source        TEXT    NOT NULL DEFAULT 'photo',   -- photo|manual
    provider      TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    raw_json      TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS weights (
    owner   TEXT    NOT NULL DEFAULT 'default',
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    date    TEXT    NOT NULL,
    value   REAL    NOT NULL,
    source  TEXT    NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS body_fat (
    owner  TEXT    NOT NULL DEFAULT 'default',
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    date   TEXT    NOT NULL,
    value  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS exercises (
    owner         TEXT    NOT NULL DEFAULT 'default',
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    date        TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    calories    REAL    NOT NULL DEFAULT 0
);

-- Per-day health export bookkeeping. requested_at is set when the Shortcut
-- fetches a day; confirmed_at is set only after it acknowledges a successful
-- HealthKit write. A confirmed day is skipped by the pending/backfill query.
CREATE TABLE IF NOT EXISTS health_exports (
    owner        TEXT NOT NULL,
    date         TEXT NOT NULL,
    requested_at INTEGER,
    confirmed_at INTEGER,
    PRIMARY KEY (owner, date)
);
CREATE INDEX IF NOT EXISTS idx_health_exports_owner ON health_exports(owner, date);

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

-- Indexes that do NOT reference owner are safe to create during executescript on
-- both fresh and upgraded databases. The owner-composite indexes are created in
-- _migrate() AFTER any ALTER TABLE ADD COLUMN owner has run.
CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(date);
CREATE INDEX IF NOT EXISTS idx_est_field ON estimates(field);

CREATE TABLE IF NOT EXISTS meal_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id     INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL DEFAULT 0,        -- display order within the meal
    name        TEXT    NOT NULL DEFAULT '',       -- generic food label
    detail      TEXT    NOT NULL DEFAULT '',       -- brand / variant / specifics
    grams       REAL    NOT NULL DEFAULT 0,
    calories    REAL    NOT NULL DEFAULT 0,
    protein     REAL    NOT NULL DEFAULT 0,
    carbs       REAL    NOT NULL DEFAULT 0,
    fat         REAL    NOT NULL DEFAULT 0,
    fiber       REAL    NOT NULL DEFAULT 0    -- subset OF carbs, not extra
);
CREATE INDEX IF NOT EXISTS idx_items_meal ON meal_items(meal_id, ord);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # One connection PER THREAD. A single connection shared by the threaded
        # HTTP server raised "InterfaceError: bad parameter or other API misuse"
        # under concurrent reads; per-thread connections + WAL let readers run
        # in parallel with the (locked) writer.
        self._local = threading.local()
        with _WRITE_LOCK:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
            self._local.depth = 0
        return conn

    @contextlib.contextmanager
    def transaction(self):
        """Group several writes into one atomic commit (rolled back on error)."""
        with _WRITE_LOCK:
            conn = self._conn
            self._local.depth += 1
            try:
                yield
            except BaseException:
                self._local.depth -= 1
                if self._local.depth == 0:
                    conn.rollback()
                raise
            self._local.depth -= 1
            if self._local.depth == 0:
                conn.commit()

    def _migrate(self):
        """Idempotent upgrades for databases created before per-user scoping.

        CREATE TABLE IF NOT EXISTS won't add columns to existing tables, so we
        detect and ALTER. Then create the owner-composite indexes unconditionally
        (they can't live in SCHEMA because executescript runs before any ALTER).
        Safe to run every startup.
        """
        for tbl in ("meals", "weights", "body_fat", "exercises"):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({tbl})")}
            if "owner" not in cols:
                self._conn.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN owner TEXT NOT NULL DEFAULT 'default'")
        # fiber arrived later and is a SUBSET of carbs (carbs stay the total), so
        # existing rows default to 0 rather than needing any backfill maths.
        for tbl in ("meals", "meal_items"):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({tbl})")}
            if "fiber" not in cols:
                self._conn.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN fiber REAL NOT NULL DEFAULT 0")
        # Items now carry a generic `name` plus an optional `detail` line for
        # brand/variant wording. Existing rows keep their name and default to
        # an empty detail; suggestions derive one when the name has specifics.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(meal_items)")}
        if "detail" not in cols:
            self._conn.execute(
                "ALTER TABLE meal_items ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
        # Drop every legacy UNIQUE index that keys on date WITHOUT owner, then
        # (re)create the owner-scoped ones. Pre-multi-user databases enforced one
        # row per date across the whole household; once several OIDC users share
        # the file that constraint makes a second user's weigh-in fail with
        # "UNIQUE constraint failed: weights.date" even though the upsert targets
        # (owner, date). Detecting by columns -- rather than dropping a fixed list
        # of names -- means this also cleans up whatever those indexes were called.
        for tbl, want in (("meals", ("owner", "date")),
                          ("weights", ("owner", "date")),
                          ("body_fat", ("owner", "date")),
                          ("exercises", ("owner", "date"))):
            # Materialize the list first: iterating a live cursor over
            # sqlite_master while issuing DROP INDEX on the SAME connection
            # leaves the schema locked ("database table is locked"), which
            # crashed startup on every restart until the index was gone.
            existing = self._conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (tbl,)).fetchall()
            for row in existing:
                name, sql = row[0], row[1].upper()
                if not sql.startswith("CREATE UNIQUE"):
                    continue                       # plain indexes are harmless
                cols = {c.strip().strip("\"`").lower() for c in
                        sql[sql.index("(") + 1:sql.rindex(")")].split(",")}
                # Compare case-insensitively: sqlite_master echoes identifiers as
                # written (often uppercased by the parser), so a case-sensitive
                # match against ("owner","date") fails and would drop the GOOD
                # owner-scoped index on every restart.
                if cols >= set(want):
                    continue                       # already owner-scoped: keep it
                # Identifier comes from sqlite_master, not user input; quote it
                # defensively anyway.
                self._conn.execute('DROP INDEX IF EXISTS "%s"' % name.replace('"', ""))
                print(f"[migrate] dropped legacy unique index {name} on {tbl}{cols}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_meals_owner ON meals(owner, date)")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_weights_owner_date ON weights(owner, date)")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bf_owner_date ON body_fat(owner, date)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ex_owner_date ON exercises(owner, date)")

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
        with self.transaction():
            return self._conn.execute(sql, params).lastrowid

    def executemany(self, sql: str, seq):
        with self.transaction():
            self._conn.executemany(sql, seq)

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
                 fiber: float = 0.0, confidence: float, photo_path, source: str,
                 provider: str, model: str, raw_json: str,
                 owner: str = "default") -> int:
        return self.execute(
            "INSERT INTO meals(owner,ts,date,kind,description,calories,protein,carbs,fat,"
            "fiber,confidence,photo_path,source,provider,model,raw_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, ts, date, kind, description, calories, protein, carbs, fat,
             fiber or 0.0, confidence, photo_path, source, provider, model,
             raw_json, int(time.time())),
        )

    def update_meal(self, meal_id: int, owner: str = "default", **fields) -> None:
        allowed = {"kind", "description", "calories", "protein", "carbs", "fat", "fiber"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ",".join(f"{k}=?" for k in sets)
        self.execute(f"UPDATE meals SET {cols} WHERE id=? AND owner=?",
                     (*sets.values(), meal_id, owner))

    def get_meal(self, meal_id: int, owner: str = "default"):
        return self.one("SELECT * FROM meals WHERE id=? AND owner=?", (meal_id, owner))

    def delete_meal(self, meal_id: int, owner: str = "default") -> sqlite3.Row | None:
        with self.transaction():
            row = self.get_meal(meal_id, owner)
            if row:
                self.execute("DELETE FROM estimates WHERE meal_id=?", (meal_id,))
                self.execute("DELETE FROM meals WHERE id=? AND owner=?", (meal_id, owner))
        return row

    def meals_for_date(self, date: str, owner: str = "default") -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM meals WHERE date=? AND owner=? ORDER BY ts", (date, owner))

    # -- meal components (items) ----------------------------------------
    def replace_meal_items(self, meal_id: int, items: list[dict]) -> dict:
        """Overwrite a meal's components and recompute its cached totals.

        Returns the summed {calories,protein,carbs,fat,fiber} so callers can persist
        them onto the meals row too. Totals are ALWAYS the sum of items when any
        items exist, keeping meal.total == SUM(components) as an invariant.
        """
        rows = []
        tot = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "fiber": 0.0}
        for i, it in enumerate(items):
            g = float(it.get("grams") or 0)
            cal = float(it.get("calories") or 0)
            p = float(it.get("protein") or 0)
            c = float(it.get("carbs") or 0)
            f = float(it.get("fat") or 0)
            # fiber is reported INSIDE carbs, so it is summed for display only and
            # never added on top of the carb total.
            fb = float(it.get("fiber") or 0)
            rows.append((meal_id, i, str(it.get("name") or "")[:120],
                         str(it.get("detail") or "")[:120], g, cal, p, c, f, fb))
            tot["calories"] += cal; tot["protein"] += p
            tot["carbs"] += c; tot["fat"] += f; tot["fiber"] += fb
        with self.transaction():               # never leave a meal half-replaced
            self.execute("DELETE FROM meal_items WHERE meal_id=?", (meal_id,))
            if rows:
                self.executemany(
                    "INSERT INTO meal_items(meal_id,ord,name,detail,grams,calories,protein,"
                    "carbs,fat,fiber) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        return {k: round(v, 1) for k, v in tot.items()}

    def items_for_meal(self, meal_id: int) -> list[dict]:
        return [dict(r) for r in self.query(
            "SELECT id,name,detail,grams,calories,protein,carbs,fat,fiber FROM meal_items "
            "WHERE meal_id=? ORDER BY ord", (meal_id,))]

    def items_for_date(self, date: str, owner: str = "default") -> dict:
        """{meal_id: [items]} for every meal the owner logged on a date."""
        out: dict[int, list] = {}
        for r in self.query(
                "SELECT mi.* FROM meal_items mi JOIN meals m ON m.id=mi.meal_id "
                "WHERE m.date=? AND m.owner=? ORDER BY mi.meal_id, mi.ord",
                (date, owner)):
            out.setdefault(r["meal_id"], []).append({
                "id": r["id"], "name": r["name"], "detail": r["detail"],
                "grams": r["grams"],
                "calories": r["calories"], "protein": r["protein"],
                "carbs": r["carbs"], "fat": r["fat"],
                "fiber": (r["fiber"] if "fiber" in r.keys() else 0)})
        return out

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
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
