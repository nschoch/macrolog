#!/usr/bin/env python3
"""Regression tests for weight-trend smoothing and adaptive target safety.

Run: python3 tests/adaptive_test.py
"""
import os
import shutil
import sys
import tempfile
import time
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.adaptive import weight_trend, adapt_targets, bmr_from_formula  # noqa: E402
from app.database import Database  # noqa: E402

PASS, FAIL = [], []
TODAY = date(2026, 10, 31)
OWNER = "test"


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    (PASS if ok else FAIL).append(name)


def make_db():
    work = tempfile.mkdtemp(prefix="macrolog-adaptive-")
    return work, Database(os.path.join(work, "macrolog.sqlite"))


def add_day(db, day, weight, calories=2000, owner=OWNER):
    ts = int(time.time())
    db.set_weight(day.isoformat(), ts, weight, owner)
    db.add_meal(date=day.isoformat(), ts=ts, kind="lunch",
                description="test meal", calories=calories, protein=100,
                carbs=200, fat=60, fiber=20, confidence=0.8,
                photo_path=None, source="manual", provider="test",
                model="test", raw_json="{}", owner=owner)


def base_settings(today=TODAY, target=2400):
    return {
        "sex": "male", "age": 35, "height_cm": 178, "activity_factor": 1.35,
        "weekly_rate_kg": -0.5, "starting_weight_kg": 80,
        "start_date": (today - timedelta(days=14)).isoformat(),
        "calorie_target": target, "auto_adapt": True,
        "calorie_target_locked": False, "target_adjusted_on": None,
    }


def history(db, days=15, weight_fn=lambda i: 80.0, calories=2000, owner=OWNER):
    for i in range(days):
        add_day(db, TODAY - timedelta(days=days - 1 - i), weight_fn(i), calories, owner)


def main():
    # 1. Five weigh-ins / seven days must not be called reliable or adapted.
    work, db = make_db()
    try:
        for i in range(5):
            add_day(db, TODAY - timedelta(days=6 - i), 80 + (i == 4) * 1.2)
        tr = weight_trend(db.query(
            "SELECT date,value FROM weights WHERE owner=? ORDER BY date", (OWNER,)))
        check("five weigh-ins are not a reliable trend",
              tr["reliable"] is False and tr["points"] == 5, str(tr))
        s = base_settings()
        s["start_date"] = (TODAY - timedelta(days=7)).isoformat()
        r = adapt_targets(db, s, today=TODAY, owner=OWNER)
        check("short history holds the target",
              r["adjusted"] is False and s["calorie_target"] == 2400,
              str(r.get("reason")))
    finally:
        db.close(); shutil.rmtree(work, ignore_errors=True)

    # 2. A locked target is a true freeze.
    work, db = make_db()
    try:
        history(db)
        s = base_settings()
        s["calorie_target_locked"] = True
        r = adapt_targets(db, s, today=TODAY, owner=OWNER)
        check("locked target never changes",
              r["adjusted"] is False and r.get("reason") == "target locked"
              and s["calorie_target"] == 2400, str(r.get("reason")))
    finally:
        db.close(); shutil.rmtree(work, ignore_errors=True)

    # 3. Implausibly low logged intake must hold, not cut to the floor.
    work, db = make_db()
    try:
        history(db, calories=900)
        s = base_settings()
        r = adapt_targets(db, s, today=TODAY, owner=OWNER)
        check("incomplete intake logging holds the target",
              r["adjusted"] is False and "incomplete" in str(r.get("reason")).lower()
              and s["calorie_target"] == 2400, str(r.get("reason")))
    finally:
        db.close(); shutil.rmtree(work, ignore_errors=True)

    # 4. Weekly cadence: a review within seven days does not move the target.
    work, db = make_db()
    try:
        history(db)
        s = base_settings()
        s["target_adjusted_on"] = TODAY.isoformat()
        r = adapt_targets(db, s, today=TODAY, owner=OWNER)
        check("weekly cadence holds until the next review",
              r["adjusted"] is False and "next review" in str(r.get("reason")).lower()
              and s["calorie_target"] == 2400, str(r.get("reason")))
    finally:
        db.close(); shutil.rmtree(work, ignore_errors=True)

    # 5. A legitimate low proposal is smoothed and capped at 150 kcal.
    work, db = make_db()
    try:
        history(db, calories=1800)
        s = base_settings()
        r = adapt_targets(db, s, today=TODAY, owner=OWNER)
        check("target move is capped at 150 kcal",
              r["adjusted"] is True and abs(r.get("delta", 0)) <= 150,
              str(r.get("delta")))
        check("target never drops below BMR",
              s["calorie_target"] >= bmr_from_formula(s), str(s["calorie_target"]))
    finally:
        db.close(); shutil.rmtree(work, ignore_errors=True)

    # 6. One late spike on a full history cannot cause a cliff-edge cut.
    work, db = make_db()
    try:
        history(db, weight_fn=lambda i: 80.0 if i < 14 else 82.0)
        s = base_settings()
        r = adapt_targets(db, s, today=TODAY, owner=OWNER)
        check("single late outlier is smoothed, not a cliff",
              abs(r.get("delta", 0)) <= 150, str(r.get("delta")))
    finally:
        db.close(); shutil.rmtree(work, ignore_errors=True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failures:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
