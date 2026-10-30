"""Unit tests for the Jev cross-check (no network: _ask is monkeypatched).

The cross-check must never be able to break an estimate, so the interesting
cases are the failure modes: no key, transport error, malformed reply.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import jev  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ok   " if ok else "  FAIL ") + name + (f" — {detail}" if detail else ""))


def canned(plausible=0.7, kind="plate_of_food", conf=0.95, drop=None):
    def _fake(state, questions, image_path, timeout=None):
        answers = {
            "plausible": {"type": "noul", "noul": plausible},
            "kind": {"type": "choice", "choice": kind, "confidence": conf,
                     "probabilities": {kind: conf}},
        }
        if drop:
            answers.pop(drop, None)
        return {"model": "jev-test", "answers": answers}
    return _fake


IMAGES = ["/tmp/does-not-need-to-exist.jpg"]

# --- disabled without a key -------------------------------------------------
os.environ.pop("JEV_API_KEY", None)
check("unavailable without a key", jev.available() is False)
check("no key -> no call at all", jev.crosscheck(IMAGES, 300) is None)

# --- normal path ------------------------------------------------------------
os.environ["JEV_API_KEY"] = "test-key"
orig = jev._ask
try:
    jev._ask = canned(0.71)
    r = jev.crosscheck(IMAGES, 60)
    check("plausible total is not flagged", r and r["suspect"] is False, str(r))
    check("kind is reported", r and r["kind"] == "plate_of_food", str(r and r["kind"]))
    check("probability is preserved", r and r["plausible"] == 0.71, str(r and r["plausible"]))
    check("latency is recorded", r and isinstance(r["ms"], int), str(r and r["ms"]))

    # The measured calibration curve: a wildly wrong claim must be flagged.
    jev._ask = canned(0.04)
    r = jev.crosscheck(IMAGES, 2000)
    check("implausible total is flagged", r and r["suspect"] is True, str(r))
    check("reason explains why", r and "off" in (r.get("reason") or ""), str(r.get("reason")))

    # Threshold boundary (PLATE_THRESHOLD = 0.30).
    jev._ask = canned(0.29)
    check("just under the threshold is suspect", jev.crosscheck(IMAGES, 500)["suspect"] is True)
    jev._ask = canned(0.30)
    check("at the threshold is not suspect", jev.crosscheck(IMAGES, 500)["suspect"] is False)

    # A non-food photo is flagged regardless of the calorie maths.
    jev._ask = canned(0.9, kind="not_food", conf=0.88)
    r = jev.crosscheck(IMAGES, 300)
    check("non-food photo is flagged", r and r["suspect"] is True, str(r))
    check("non-food reason is specific", r and r.get("reason") == "no food visible",
          str(r.get("reason")))
    jev._ask = canned(0.9, kind="not_food", conf=0.4)
    check("low-confidence non-food is NOT flagged",
          jev.crosscheck(IMAGES, 300)["suspect"] is False)

    # A nutrition label outranks the calorie guess (labels are measured numbers).
    jev._ask = canned(0.2, kind="nutrition_label")
    r = jev.crosscheck(IMAGES, 230)
    check("label photo still reports its kind", r["kind"] == "nutrition_label", str(r["kind"]))
    check("label photo is not treated as non-food", r["suspect"] is True
          and r.get("reason") != "no food visible", str(r.get("reason")))

    # --- fail-open behaviour ------------------------------------------------
    def boom(*a, **k):
        raise OSError("network down")
    jev._ask = boom
    check("transport failure -> None (never raises)", jev.crosscheck(IMAGES, 300) is None)

    jev._ask = lambda *a, **k: None
    check("empty reply -> None", jev.crosscheck(IMAGES, 300) is None)

    jev._ask = lambda *a, **k: {"answers": {}}
    check("reply with no answers -> None", jev.crosscheck(IMAGES, 300) is None)

    jev._ask = canned()
    check("zero calories -> no call", jev.crosscheck(IMAGES, 0) is None)
    check("no images -> no call", jev.crosscheck([], 300) is None)
finally:
    jev._ask = orig

# --- the key must not leak into the module's public surface -----------------
check("module never stores the key on itself",
      "test-key" not in repr({k: v for k, v in vars(jev).items() if not k.startswith("__")}))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
