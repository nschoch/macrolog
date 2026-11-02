#!/usr/bin/env python3
"""Unit tests for generic food-name folding (app/foods.py).

These use the real labels that came back from photo/text estimates so a
regression in the grouping key fails here rather than silently hiding a
staple from the quick-log chips.

Run: python3 tests/foods_test.py
Exit 0 = every check passed.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import foods  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    (PASS if ok else FAIL).append(name)


def same(*names):
    keys = {foods.food_key(n) for n in names}
    return len(keys) == 1, keys


# ---------------------------------------------------------------- one food
check("weight and cooking method do not split oats",
      *same("Steel cut oats (cooked)", "steel cut oatmeal (dry weight 66g, cooked)",
            "cooked steel cut oats", "Steel Cut Oats Cooked"))
check("oats fold to a readable generic label",
      foods.canonicalize("steel cut oatmeal (dry weight 66g, cooked)")[0] == "Steel cut oats",
      foods.canonicalize("steel cut oatmeal (dry weight 66g, cooked)")[0])
check("brand + fat level do not split yogurt",
      *same("Chobani Nonfat Plain Greek Yogurt (protein rich)",
            "Chobani Whole Milk Plain Greek Yogurt",
            "Chobani plain Greek yogurt (protein rich)",
            "Greek Yogurt Whole Milk Plain (Chobani)"))
check("yogurt folds to its generic head noun",
      foods.canonicalize("Chobani Nonfat Plain Greek Yogurt (protein rich)")[0]
      == "Greek yogurt")
check("brand and 4% do not split cottage cheese",
      *same("Nancy's Organic Probiotic Cottage Cheese (4% milkfat)",
            "Cottage cheese", "4% cottage cheese",
            "Organic Probiotic Cottage Cheese Whole Milk (Nancy's)"))
check("cottage cheese folds to its generic head noun",
      foods.canonicalize("Nancy's Organic Probiotic Cottage Cheese (4% milkfat)")[0]
      == "Cottage cheese")
check("berry mixes and cultivars fold together",
      *same("mixed berries", "Wild blueberries (frozen, packaged)",
            "Frozen wild blueberries (1 cup, 140g)",
            "Triple Berry Wild Blueberries Blackberries & Raspberries"))
check("berry mix folds to a readable label",
      foods.canonicalize("Triple Berry Wild Blueberries Blackberries & Raspberries")[0]
      == "Berries")
check("whole / plural psyllium folds",
      *same("Whole Psyllium Husks", "psyllium husk", "Psyllium husk"))
check("brazil nut singular/plural folds",
      *same("Brazil nuts", "Brazil nut"))
check("kiwifruit and kiwi fold",
      *same("kiwifruit", "Kiwi"))
check("banana casing and 'fresh' fold",
      *same("banana", "Banana, Fresh"))
check("peanuts in shell fold",
      *same("Shelled peanuts", "peanuts in shell (shelled kernels eaten)"))

# --------------------------------------------------------------- near misses
for a, b, why in [
    ("Chicken Pot Pie", "Breaded Chicken Cutlet", "different dishes"),
    ("Greek yogurt", "Cottage cheese", "different dairy"),
    ("Banana", "Banana bread", "bread is a different food"),
    ("Peanut", "Peanut butter", "butter is a different food"),
    ("Almond", "Almond milk", "milk is a different food"),
    ("French fries", "Potato", "preparation defines the food"),
    ("Sweet potatoes", "Potatoes", "different vegetable"),
    ("Ramen", "Noodles", "ramen is not plain noodles"),
]:
    keys = {foods.food_key(a), foods.food_key(b)}
    check(f"does not merge {a!r} with {b!r} ({why})", len(keys) == 2, str(keys))

# ------------------------------------------------------------------ details
label, detail = foods.canonicalize("Chobani Nonfat Plain Greek Yogurt (protein rich)")
check("brand survives as a detail", detail == "Chobani nonfat", detail)
label, detail = foods.canonicalize("Nancy's Organic Probiotic Cottage Cheese (4% milkfat)")
check("brand/variant survives as a detail", detail == "Nancy's organic probiotic", detail)
label, detail = foods.canonicalize("Chobani Whole Milk Plain Greek Yogurt")
check("fat level survives as a detail", detail == "Chobani whole milk", detail)
label, detail = foods.canonicalize("Greek full fat yogurt")
check("fat level is folded into the detail, not the label",
      label == "Greek yogurt" and detail == "Full fat", f"{label!r} / {detail!r}")
check("fat level folds out of the grouping key",
      len({foods.food_key("Greek full fat yogurt"),
           foods.food_key("Greek yogurt"),
           foods.food_key("Chobani nonfat plain Greek yogurt")}) == 1)
label, detail = foods.canonicalize("cooked steel cut oats")
check("preparation adds no detail", detail == "", detail)
label, detail = foods.canonicalize("4% cottage cheese")
check("amount-only variants add no detail", detail == "", detail)

# -------------------------------------------------------------- quantities
for raw in ["Rise & Puff Gourmet Quesadilla - Chipotle Chicken (1 package, 121g)",
            "Sun Cruiser Pink Lemonade Vodka drink (355 ml can)",
            "1 cup (140g) of frozen blueberries"]:
    check(f"strips quantities from {raw[:32]!r}",
          not any(ch.isdigit() for ch in foods.canonicalize(raw)[0]),
          foods.canonicalize(raw)[0])

check("nested parentheses do not leak packaging text",
      foods.canonicalize("Mixed plate (1 photo(s), offline)")[0] == "Mixed plate",
      foods.canonicalize("Mixed plate (1 photo(s), offline)")[0])
check("identity-only product keeps its brand",
      foods.canonicalize("Flintstones Complete Chewable Tablet")[0]
      == "Flintstones Complete Chewable Tablet",
      foods.canonicalize("Flintstones Complete Chewable Tablet")[0])
check("empty input has a safe fallback",
      foods.canonicalize("")[0] == "Food" and foods.food_key("") == "food")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failed:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
