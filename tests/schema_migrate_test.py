#!/usr/bin/env python3
"""Regression: pre-multi-user databases must not block two users logging the
same date.

The original schema enforced UNIQUE(date) with no owner column. After OIDC was
added, set_weight() upserts ON CONFLICT(owner,date), but the stray legacy index
still applied household-wide -- so a second family member's weigh-in failed with
"IntegrityError: UNIQUE constraint failed: weights.date". _migrate() must drop
any unique index that omits owner, keep the owner-scoped one, and be safe to run
repeatedly (it runs on every process start).
Run:  python3 tests/schema_migrate_test.py
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    (PASS if ok else FAIL).append(name)


LEGACY = """
CREATE TABLE weights (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
  date TEXT NOT NULL, value REAL NOT NULL, source TEXT NOT NULL DEFAULT 'manual');
CREATE UNIQUE INDEX idx_weights_date ON weights(date);
INSERT INTO weights(ts,date,value) VALUES (1793200000,'2026-10-28',70.9);
CREATE TABLE body_fat (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
  date TEXT NOT NULL, value REAL NOT NULL);
CREATE UNIQUE INDEX idx_bf_date ON body_fat(date);
CREATE TABLE meals (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
  date TEXT NOT NULL, kind TEXT, description TEXT, calories REAL, protein REAL,
  carbs REAL, fat REAL, confidence REAL, photo_path TEXT, source TEXT,
  provider TEXT, model TEXT, raw_json TEXT, created_at INTEGER);
CREATE TABLE exercises (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
  date TEXT NOT NULL, name TEXT, calories REAL);
"""


def indexes(path, table):
    con = sqlite3.connect(path)
    names = sorted(r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,))
        if not r[0].startswith("sqlite_"))
    con.close()
    return names


def main():
    work = tempfile.mkdtemp(prefix="macrolog-mig-")
    path = os.path.join(work, "macrolog.sqlite")
    con = sqlite3.connect(path)
    con.executescript(LEGACY)
    con.commit()
    con.close()

    os.environ["MACROLOG_DATA"] = work
    from app.database import Database

    db = Database(path)
    check("legacy date-only unique index dropped",
          "idx_weights_date" not in indexes(path, "weights"), str(indexes(path, "weights")))
    check("owner-scoped unique index present",
          "idx_weights_owner_date" in indexes(path, "weights"), str(indexes(path, "weights")))
    check("body_fat legacy index also dropped",
          "idx_bf_date" not in indexes(path, "body_fat"), str(indexes(path, "body_fat")))

    # The user-facing failure: two accounts weighing in on one day.
    try:
        db.set_weight("2026-10-29", 1793300000, 71.2, owner="alice")
        db.set_weight("2026-10-29", 1793300001, 88.0, owner="bob")
        both_ok = True
    except sqlite3.IntegrityError as e:
        both_ok = False
        print("     IntegrityError:", e)
    check("two users can log the same date", both_ok)

    # Re-logging replaces only your own row.
    db.set_weight("2026-10-29", 1793300002, 71.5, owner="alice")
    rows = {(r["owner"], r["value"]) for r in
            db.query("SELECT owner,value FROM weights WHERE date='2026-10-29'")}
    check("upsert replaces own row without duplicating",
          rows == {("alice", 71.5), ("bob", 88.0)}, str(sorted(rows)))
    db.close()

    # Migration runs at every process start; it must converge, not oscillate.
    for i in range(3):
        reopen = Database(path)
        reopen.close()
    final = indexes(path, "weights")
    check("stable across repeated migrations",
          final == ["idx_weights_owner_date"], f"{final} after 3 restarts")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failures:", ", ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
