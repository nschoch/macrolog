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

from datetime import date, timedelta

# ---------------------------------------------------------------- constants
KCAL_PER_KG = 7700.0
MIN_TARGET_ADJUST = 50.0     # never move the target by a trivially small amount
MAX_WEEKLY_CUT = 1500.0      # safety clamp on how aggressive a cut can get
MAX_WEEKLY_GAIN = 1000.0     # safety clamp on a bulk
SMOOTHING = 0.6              # fraction of the computed correction to apply
TREND_WINDOW_DAYS = 21       # regression window for the weight slope
MIN_TREND_POINTS = 4         # enough data before we trust the slope

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


def weight_trend(rows) -> dict:
    """Regression over recent weights. rows = iterable of (date_str, kg) or Rows.

    Uses only the trailing TREND_WINDOW_DAYS but requires MIN_TREND_POINTS
    samples before reporting reliable=True.
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

    latest_day = max(d for d, _ in parsed)
    cutoff = latest_day - timedelta(days=TREND_WINDOW_DAYS)
    window = [(d, v) for d, v in parsed if d >= cutoff]
    if len(window) < 2:
        window = parsed[-min(len(parsed), 6):]

    base = min(d for d, _ in window)
    pts = [((d - base).days, v) for d, v in window]
    slope_per_day = linear_slope(pts)

    # Smoothed current weight: fitted value at the newest day, not the raw
    # last weigh-in, then blended toward it so the number stays believable.
    if slope_per_day is not None:
        n = float(len(pts))
        mx = sum(x for x, _ in pts) / n
        my = sum(y for _, y in pts) / n
        intercept = my - slope_per_day * mx
        smoothed = intercept + slope_per_day * pts[-1][0]
    else:
        smoothed = pts[-1][1]
    smoothed = 0.75 * smoothed + 0.25 * pts[-1][1]

    days = (max(d for d, _ in window) - min(d for d, _ in window)).days
    return {
        "slope_kg_per_week": round(slope_per_day * 7.0, 3) if slope_per_day is not None else None,
        "current_kg": round(smoothed, 2),
        "points": len(window),
        "days_covered": days,
        "reliable": len(window) >= MIN_TREND_POINTS and days >= 7,
    }


# ------------------------------------------------------------- energy needs
def tdee_from_formula(settings: dict, weight_kg=None):
    """Mifflin-St Jeor baseline, used only to seed the first target."""
    sex = settings.get("sex", "male")
    age = float(settings.get("age") or 30)
    height_cm = float(settings.get("height_cm") or 175)
    activity = float(settings.get("activity_factor") or 1.35)
    w = weight_kg or float(settings.get("starting_weight_kg") or 80)
    base = 10.0 * w + 6.25 * height_cm - 5.0 * age
    base += 5 if sex == "male" else -161
    return round(base * activity)


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

    return {"calories": round(calorie_target), "protein": protein_g,
            "carbs": carbs_g, "fat": fat_g}


def adapt_targets(db, settings: dict, today=None) -> dict:
    """Core loop: reconcile logged intake against observed weight change.

    Reads the rolling weight trend plus recent average logged intake, then
    moves the calorie target so predicted and observed rates converge. The
    caller persists settings["calorie_target"] when adjusted is True.
    """
    today = today or date.today()
    start_date = settings.get("start_date")
    if start_date:
        try:
            if (today - date.fromisoformat(start_date)).days < 3:
                return {"reason": "too early", "adjusted": False, "trend": {}}
        except ValueError:
            pass

    rows = db.query(
        "SELECT date, value FROM weights WHERE date <= ? ORDER BY date",
        (today.isoformat(),))
    trend = weight_trend(rows)

    # Average logged intake over the same window the weight slope uses, so the
    # two numbers describe the identical period. COUNT(DISTINCT date) guards
    # against one heavily-logged day outweighing sparse days.
    avg = db.one(
        "SELECT AVG(day_total) a, COUNT(*) d FROM ("
        "  SELECT date, SUM(calories) day_total FROM meals"
        "  WHERE date > ? AND date <= ? GROUP BY date)",
        ((today - timedelta(days=TREND_WINDOW_DAYS)).isoformat(), today.isoformat()))
    logged_avg = avg["a"] if avg else None
    logged_days = int(avg["d"]) if avg else 0

    result = {"trend": trend, "logged_avg": round(logged_avg) if logged_avg else None,
              "logged_days": logged_days, "adjusted": False}

    weekly_rate = float(settings.get("weekly_rate_kg") or 0.0)
    seed = tdee_from_formula(settings, trend["current_kg"])

    if not trend["reliable"] or not logged_avg or logged_days < MIN_TREND_POINTS:
        result["reason"] = "insufficient data"
        if seed:
            result["target"] = round(seed + weekly_rate * KCAL_PER_KG / 7.0)
        return result

    # Solve for TRUE maintenance directly instead of nudging the last target.
    #   observed_daily_change = (intake - maintenance) / KCAL_PER_KG * ... 
    # Rearranged: maintenance = intake - daily_observed_energy_imbalance.
    # This is a fixed point: re-running it on unchanged data returns the same
    # answer, so repeated calls cannot compound into a runaway target.
    observed_kcal_per_day = trend["slope_kg_per_week"] * KCAL_PER_KG / 7.0
    maintenance_est = logged_avg - observed_kcal_per_day

    # Blend with the formula estimate; the more real data we have, the more we
    # trust observation over the population-average equation.
    data_weight = min(0.85, logged_days / 21.0)
    baseline = seed or maintenance_est
    true_maintenance = (data_weight * maintenance_est + (1 - data_weight) * baseline)

    proposed = true_maintenance + weekly_rate * KCAL_PER_KG / 7.0

    # Guard against absurd results from messy input data.
    floor = (seed or 1600) * 0.55
    ceiling = (seed or 2600) * 1.55
    hard_floor = max(1200.0, floor)
    if not (hard_floor <= proposed <= ceiling):
        result.update(reason="clamped to safe range",
                      target=round(min(max(proposed, hard_floor), ceiling)),
                      delta=0)
        settings["calorie_target"] = round(min(max(proposed, hard_floor), ceiling))
        return result

    current = settings.get("calorie_target")
    if current is None:
        # First run: adopt the computed value outright rather than comparing
        # against nothing (which would strand the target as unset).
        settings["calorie_target"] = round(proposed)
        result.update(adjusted=True, target=round(proposed), delta=0,
                      maintenance=round(true_maintenance),
                      reason=f"initial estimate from {logged_days} logged days")
        return result

    current = float(current)
    if abs(proposed - current) < max(MIN_TARGET_ADJUST, abs(current) * 0.03):
        result.update(reason="within deadband", target=round(current), delta=0)
        return result

    settings["calorie_target"] = round(proposed)
    result.update(adjusted=True, target=round(proposed),
                  delta=round(proposed - current),
                  maintenance=round(true_maintenance),
                  reason=f"observed {trend['slope_kg_per_week']:+.2f} kg/wk on "
                         f"{round(logged_avg)} kcal/day intake vs goal {weekly_rate:+.2f} kg/wk")
    return result


# ------------------------------------------------- estimate calibration
def calibration_multipliers(db, field: str = "calories", min_samples: int = 3) -> dict:
    """Per-food-group ratio of actual/predicted, shrunk toward 1.0."""
    rows = db.query(
        "SELECT m.description, m.kind, e.predicted, e.actual "
        "FROM estimates e JOIN meals m ON m.id = e.meal_id WHERE e.field = ?",
        (field,))
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


def accuracy_summary(db) -> dict:
    """How good is the estimator, per field? Drives the UI trust badge."""
    out = {}
    for field in ("calories", "protein", "carbs", "fat"):
        rows = db.query(
            "SELECT rel_error, (actual-predicted)/predicted AS s FROM estimates "
            "WHERE field=? AND predicted>0 ORDER BY logged_at DESC LIMIT 50",
            (field,))
        errs = [r["rel_error"] for r in rows]
        if not errs:
            out[field] = {"n": 0, "mape": None, "bias": None}
            continue
        out[field] = {
            "n": len(errs),
            "mape": round(100 * sum(errs) / len(errs), 1),
            "bias": round(100 * sum(r["s"] for r in rows) / len(rows), 1),
        }
    return out
