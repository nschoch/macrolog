"""Adaptive energy targets and estimate calibration.

Two feedback loops, both borrowed in spirit from MacroFactor:

1. WEIGHT -> TARGETS ("the treatment"). A least-squares slope over the last
   ~3 weeks of weight estimates the true rate of change, immune to daily water
   noise. Comparing that rate against the user's goal reveals whether logged
   intake is under-reported, and the calorie target is nudged until observed
   and desired rates agree.

2. ESTIMATE -> CORRECTION. Every time a photo estimate is edited, the pair is
   stored. Per-food-group multipliers learned from that history are applied to
   future estimates, so if the model systematically guesses low on pasta, it
   stops guessing low on pasta.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta

# ---------------------------------------------------------------- constants
KCAL_PER_KG = 7700.0
MIN_TARGET_ADJUST = 50.0     # never move the target by a trivially small amount
MAX_TARGET_STEP = 150.0      # max change per weekly review
SMOOTHING = 0.6              # fraction of the computed correction to apply
TREND_WINDOW_DAYS = 21       # regression window for the weight slope
MIN_TREND_POINTS = 8         # enough weigh-ins before we trust the slope
MIN_TREND_DAYS = 14          # at least two weeks of trend data
MIN_LOGGED_DAYS = 10         # intake coverage before trusting maintenance
MIN_INTAKE_COVERAGE = 0.70    # at least 70% of days in the window
MIN_INTAKE_BMR_FRACTION = 0.80  # below this, intake logging is likely incomplete

FOOD_GROUPS = [
    ("alcohol", ("beer", "wine", "liquor", "cocktail", "vodka", "whiskey",
                 "tequila", "rum", "cider", "sake", "ipa", "lager", "stout",
                 "ale", "shot", "margarita", "gin", "bourbon")),
    ("fried_fast", ("fry", "fries", "burger", "pizza", "chicken nugget",
                    "taco", "burrito", "doner", "kebab", "corn dog", "onion ring")),
    ("sauces_dressed", ("dressing", "sauce", "gravy", "ketchup", "mayo",
                        "aioli", "curry", "stew", "marinara", "alfredo", "ranch")),
    ("oily_plant", ("avocado", "olive", "nut butter", "peanut butter", "almond",
                    "cashew", "walnut", "pecan", "coconut", "salmon", "tuna",
                    "mackerel", "sardine", "herring", "chia", "flax")),
    ("sweets", ("cake", "cookie", "ice cream", "chocolate", "candy", "donut",
                "pastry", "croissant", "muffin", "pie", "brownie", "gelato",
                "yogurt parfait", "granola", "honey", "syrup", "juice", "soda",
                "smoothie", "latte", "frappuccino", "bubble tea")),
    ("protein_plain", ("chicken breast", "turkey", "lean beef", "pork loin",
                       "egg", "tofu", "tempeh", "white fish", "cod", "tilapia",
                       "shrimp", "prawn", "whey", "protein shake",
                       "cottage cheese", "greek yogurt", "edamame", "seitan")),
    ("starch", ("rice", "pasta", "noodle", "potato", "sweet potato", "bread",
                "oat", "quinoa", "couscous", "wrap", "tortilla", "bagel",
                "cereal", "grits", "polenta", "barley", "farro", "ramen",
                "pancake", "waffle")),
    ("produce", ("salad", "lettuce", "spinach", "kale", "broccoli", "cauliflower",
                 "carrot", "cucumber", "tomato", "pepper", "zucchini", "squash",
                 "bean sprout", "green bean", "asparagus", "brussels", "cabbage",
                 "onion", "garlic", "fruit", "apple", "banana", "orange", "berry",
                 "strawberr", "blueberr", "raspberr", "mango", "pineapple",
                 "watermelon", "grape", "kiwi", "pear", "peach", "plum")),
]


def food_group(text: str) -> str:
    """Map free text to a coarse group used for bias correction."""
    low = (text or "").lower()
    for group, keys in FOOD_GROUPS:
        if any(k in low for k in keys):
            return group
    return "other"


# ------------------------------------------------------------ weight trend
def linear_slope(points):
    """Least-squares slope (units per x-unit). None if underdetermined."""
    pts = [(x, y) for x, y in points if x is not None and y is not None]
    if len(pts) < 2:
        return None
    n = float(len(pts))
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    return sxy / sxx


def theil_sen_slope(points):
    """Median of pairwise slopes: robust to one bad weigh-in."""
    pts = [(x, y) for x, y in points if x is not None and y is not None]
    slopes = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            dx = pts[j][0] - pts[i][0]
            if dx:
                slopes.append((pts[j][1] - pts[i][1]) / dx)
    return statistics.median(slopes) if slopes else None


def weight_trend(rows) -> dict:
    """Robust trend over recent weights. rows = (date_str, kg) or DB Rows.

    Theil-Sen rather than least squares, so a single water-weight spike cannot
    dominate the slope. Reliability now requires two weeks and eight weigh-ins;
    the old 4-point/7-day gate trusted very thin data.
    """
    parsed = []
    for r in rows:
        try:
            if hasattr(r, "keys"):
                d, v = r["date"], r["value"]
            else:
                d, v = r[0], r[1]
            parsed.append((date.fromisoformat(str(d)[:10]), float(v)))
        except (ValueError, TypeError, KeyError, IndexError):
            continue
    if not parsed:
        return {"slope_kg_per_week": None, "current_kg": None, "points": 0,
                "days_covered": 0, "reliable": False}
    parsed.sort(key=lambda x: x[0])

    latest_day = max(d for d, _ in parsed)
    cutoff = latest_day - timedelta(days=TREND_WINDOW_DAYS)
    window = [(d, v) for d, v in parsed if d >= cutoff]
    if len(window) < 2:
        window = parsed[-min(len(parsed), 6):]

    base = min(d for d, _ in window)
    pts = [((d - base).days, v) for d, v in window]
    slope_per_day = theil_sen_slope(pts)

    if slope_per_day is not None:
        intercept = statistics.median(v - slope_per_day * x for x, v in pts)
        smoothed = intercept + slope_per_day * pts[-1][0]
        # Blend with the median of the last three readings, not just the raw
        # latest one, so a one-day spike does not become "current weight".
        recent = statistics.median(v for _, v in window[-3:])
        smoothed = 0.75 * smoothed + 0.25 * recent
    else:
        smoothed = statistics.median(v for _, v in window[-3:])

    days = (max(d for d, _ in window) - min(d for d, _ in window)).days
    return {
        "slope_kg_per_week": round(slope_per_day * 7.0, 3) if slope_per_day is not None else None,
        "current_kg": round(smoothed, 2),
        "points": len(window),
        "days_covered": days,
        "reliable": len(window) >= MIN_TREND_POINTS and days >= MIN_TREND_DAYS,
    }


# ------------------------------------------------------------- energy needs
def bmr_from_formula(settings: dict, weight_kg=None):
    """Mifflin-St Jeor BMR. This is the safety floor for a calorie target."""
    sex = settings.get("sex", "male")
    age = float(settings.get("age") or 30)
    height_cm = float(settings.get("height_cm") or 175)
    w = weight_kg or float(settings.get("starting_weight_kg") or 80)
    base = 10.0 * w + 6.25 * height_cm - 5.0 * age
    base += 5 if sex == "male" else -161
    return round(base)


def tdee_from_formula(settings: dict, weight_kg=None):
    """Mifflin-St Jeor baseline, used only to seed the first target."""
    activity = float(settings.get("activity_factor") or 1.35)
    return round(bmr_from_formula(settings, weight_kg) * activity)


def macronutrient_targets(calorie_target: float, settings: dict) -> dict:
    """Protein by bodyweight, fat as % of calories, carbs fill the rest."""
    weight = settings.get("_current_weight_kg") or float(
        settings.get("starting_weight_kg") or 80)
    g_per_kg = float(settings.get("protein_g_per_kg") or 1.8)
    protein_g = round(g_per_kg * weight)
    fat_pct = float(settings.get("fat_calorie_pct") or 0.25)
    fat_g = round(calorie_target * fat_pct / 9.0)

    remaining = calorie_target - (protein_g * 4 + fat_g * 9)
    if remaining < 0:
        # Protein alone nearly fills the budget; give fat a floor and zero carbs.
        fat_g = round(max(30.0, (calorie_target - protein_g * 4) * fat_pct / 9.0))
        remaining = max(0.0, calorie_target - protein_g * 4 - fat_g * 9)
    carbs_g = round(remaining / 4.0)

    # Fiber target: 14 g per 1000 kcal (US Dietary Reference Intake), which scales
    # with the calorie target instead of being a fixed number that is unreachable
    # on a cut. Fiber is a SUBSET of carbs above -- this is a floor to aim for,
    # not a fourth macro to add into the calorie budget.
    fiber_g = round(calorie_target / 1000.0 * 14.0)

    return {"calories": round(calorie_target), "protein": protein_g,
            "carbs": carbs_g, "fat": fat_g, "fiber": fiber_g}


def adapt_targets(db, settings: dict, today=None, owner: str = "default",
                  force: bool = False) -> dict:
    """Core loop: reconcile logged intake against observed weight change.

    Safety rules (in order):
      1. a locked target is never changed;
      2. no adaptation before 14 days / 8 weigh-ins / 10 logged days;
      3. hold when intake logging looks incomplete;
      4. monthly? no -> weekly cadence, 60% smoothing, max 150 kcal per move;
      5. never propose below BMR (Mifflin-St Jeor).
    """
    today = today or date.today()
    rows = db.query(
        "SELECT date, value FROM weights WHERE date <= ? AND owner=? ORDER BY date",
        (today.isoformat(), owner))
    trend = weight_trend(rows)
    weekly_rate = float(settings.get("weekly_rate_kg") or 0.0)
    seed = tdee_from_formula(settings, trend["current_kg"])
    bmr = bmr_from_formula(settings, trend["current_kg"])
    formula_target = round(seed + weekly_rate * KCAL_PER_KG / 7.0)
    result = {"trend": trend, "adjusted": False, "seed": seed, "bmr": bmr,
              "target": formula_target}

    if settings.get("calorie_target_locked"):
        result.update(reason="target locked", target=settings.get("calorie_target"))
        return result

    start_date = settings.get("start_date")
    elapsed = None
    if start_date:
        try:
            elapsed = (today - date.fromisoformat(str(start_date)[:10])).days
        except ValueError:
            elapsed = None
    if elapsed is not None and elapsed < MIN_TREND_DAYS:
        result["reason"] = f"need {MIN_TREND_DAYS} days of data (have {max(0, elapsed)})"
        return result

    # Average logged intake over the same window the weight slope uses, so the
    # two numbers describe the identical period. COUNT(DISTINCT date) guards
    # against one heavily-logged day outweighing sparse days.
    avg = db.one(
        "SELECT AVG(day_total) a, COUNT(*) d FROM ("
        "  SELECT date, SUM(calories) day_total FROM meals"
        "  WHERE date > ? AND date <= ? AND owner=? GROUP BY date)",
        ((today - timedelta(days=TREND_WINDOW_DAYS)).isoformat(), today.isoformat(), owner))
    logged_avg = avg["a"] if avg else None
    logged_days = int(avg["d"]) if avg else 0
    result.update(logged_avg=round(logged_avg) if logged_avg else None,
                  logged_days=logged_days)

    if not trend["reliable"]:
        result["reason"] = (f"need {MIN_TREND_POINTS} weigh-ins over "
                            f"{MIN_TREND_DAYS} days")
        return result
    if not logged_avg or logged_days < MIN_LOGGED_DAYS:
        result["reason"] = f"need {MIN_LOGGED_DAYS} logged-intake days (have {logged_days})"
        return result

    span = max(MIN_TREND_DAYS, min(TREND_WINDOW_DAYS,
                                  elapsed if elapsed is not None
                                  else max(MIN_TREND_DAYS, trend["days_covered"])))
    coverage = logged_days / float(span)
    result["intake_coverage"] = round(coverage, 2)
    if coverage < MIN_INTAKE_COVERAGE:
        result["reason"] = f"intake logged on {logged_days} of {span} days"
        return result
    if logged_avg < bmr * MIN_INTAKE_BMR_FRACTION:
        result["reason"] = (f"logged intake looks incomplete "
                            f"({round(logged_avg)} kcal/day); holding")
        return result

    if not force and settings.get("target_adjusted_on"):
        try:
            since = (today - date.fromisoformat(
                str(settings["target_adjusted_on"])[:10])).days
        except ValueError:
            since = None
        if since is not None and since < 7:
            result.update(reason=f"next review in {7 - since} days",
                          target=settings.get("calorie_target"))
            return result

    observed_kcal_per_day = trend["slope_kg_per_week"] * KCAL_PER_KG / 7.0
    maintenance_est = logged_avg - observed_kcal_per_day
    if maintenance_est < bmr * 0.85:
        result.update(reason="trend and intake disagree; holding target",
                      maintenance=round(maintenance_est),
                      target=settings.get("calorie_target"))
        return result

    # Blend with the formula estimate; the more real data we have, the more we
    # trust observation over the population-average equation.
    data_weight = min(0.85, logged_days / 21.0)
    baseline = seed or maintenance_est
    true_maintenance = (data_weight * maintenance_est + (1 - data_weight) * baseline)
    proposed = true_maintenance + weekly_rate * KCAL_PER_KG / 7.0

    # The safety floor is BMR, not a fraction of TDEE. A target below BMR is
    # what made a single heavy weigh-in look like "near starvation".
    floor = max(1200.0, float(bmr))
    ceiling = (seed or 2600) * 1.55
    clamped = min(max(proposed, floor), ceiling)
    result.update(maintenance=round(true_maintenance), proposed=round(proposed),
                  floor=round(floor), ceiling=round(ceiling))

    current = settings.get("calorie_target")
    if current is None:
        settings["calorie_target"] = round(clamped)
        settings["target_adjusted_on"] = today.isoformat()
        result.update(adjusted=True, target=round(clamped), delta=0,
                      reason=f"initial estimate from {logged_days} logged days")
        return result

    current = float(current)
    if abs(clamped - current) < max(MIN_TARGET_ADJUST, abs(current) * 0.03):
        result.update(reason="within deadband", target=round(current), delta=0)
        return result

    desired = current + SMOOTHING * (clamped - current)
    step = max(-MAX_TARGET_STEP, min(MAX_TARGET_STEP, desired - current))
    new_target = round(current + step)
    if abs(new_target - current) < MIN_TARGET_ADJUST:
        result.update(reason="within deadband", target=round(current), delta=0)
        return result

    settings["calorie_target"] = new_target
    settings["target_adjusted_on"] = today.isoformat()
    reason = (f"observed {trend['slope_kg_per_week']:+.2f} kg/wk on "
              f"{round(logged_avg)} kcal/day vs goal {weekly_rate:+.2f} kg/wk")
    if abs(step) >= MAX_TARGET_STEP:
        reason += f"; capped at {int(MAX_TARGET_STEP)} kcal"
    result.update(adjusted=True, target=new_target,
                  delta=round(new_target - current), reason=reason)
    return result


# ------------------------------------------------- estimate calibration
def calibration_multipliers(db, field: str = "calories", min_samples: int = 3,
                            owner: str = "default") -> dict:
    """Per-food-group ratio of actual/predicted, shrunk toward 1.0."""
    rows = db.query(
        "SELECT m.description, m.kind, e.predicted, e.actual "
        "FROM estimates e JOIN meals m ON m.id = e.meal_id "
        "WHERE e.field = ? AND m.owner = ?",
        (field, owner))
    buckets = {}
    for r in rows:
        if not r["predicted"]:
            continue
        group = food_group(f"{r['description']} {r['kind']}")
        buckets.setdefault(group, []).append(r["actual"] / r["predicted"])

    out = {}
    for group, ratios in buckets.items():
        n = len(ratios)
        if n < min_samples:
            continue
        mean = sum(ratios) / n
        # James-Stein style shrinkage: trust a group more as samples accumulate.
        shrunk = 1.0 + (n / (n + 5.0)) * (mean - 1.0)
        out[group] = round(min(2.0, max(0.5, shrunk)), 3)
    return out


def record_correction(db, meal_id: int, old: dict, new: dict, ts_now: int) -> int:
    """Store estimate-vs-actual pairs whenever a logged meal is edited."""
    rows = []
    for field in ("calories", "protein", "carbs", "fat"):
        pred, act = float(old.get(field) or 0), float(new.get(field) or 0)
        if pred <= 0 or act <= 0 or abs(act - pred) < 1e-6:
            continue
        rows.append((meal_id, field, pred, act, abs(act - pred),
                     abs(act - pred) / pred, ts_now))
    if rows:
        db.executemany(
            "INSERT INTO estimates(meal_id,field,predicted,actual,abs_error,"
            "rel_error,logged_at) VALUES(?,?,?,?,?,?,?)", rows)
    return len(rows)


def apply_calibration(meal: dict, multipliers: dict):
    """Scale an estimate by learned per-group bias. Returns (meal, changed)."""
    if not multipliers:
        return meal, False
    group = food_group(f"{meal.get('description', '')} {meal.get('kind', '')}")
    mult = multipliers.get(group)
    if not mult or abs(mult - 1.0) < 0.02:
        return meal, False
    adjusted = dict(meal)
    for field in ("calories", "protein", "carbs", "fat"):
        adjusted[field] = round(float(meal.get(field) or 0) * mult, 1)
    adjusted["notes"] = (meal.get("notes", "") +
                         f" [calibrated {group} x{mult}]").strip()
    return adjusted, True


def accuracy_summary(db, owner: str = "default") -> dict:
    """How good is the estimator, per field? Drives the UI trust badge."""
    out = {}
    for field in ("calories", "protein", "carbs", "fat"):
        rows = db.query(
            "SELECT e.rel_error AS rel, (e.actual-e.predicted)/e.predicted AS s "
            "FROM estimates e JOIN meals m ON m.id=e.meal_id "
            "WHERE e.field=? AND e.predicted>0 AND m.owner=? "
            "ORDER BY e.logged_at DESC LIMIT 50",
            (field, owner))
        errs = [r["rel"] for r in rows]
        if not errs:
            out[field] = {"n": 0, "mape": None, "bias": None}
            continue
        out[field] = {
            "n": len(errs),
            "mape": round(100 * sum(errs) / len(errs), 1),
            "bias": round(100 * sum(r["s"] for r in rows) / len(rows), 1),
        }
    return out
