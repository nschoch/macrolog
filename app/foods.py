"""Generic food names for quick-log suggestions and per-item details.

The estimator names a scan after what it sees -- brand, weight, and cooking
method included, e.g. ``Chobani Nonfat Plain Greek Yogurt (protein rich)``.
Because the same bowl is re-scanned with a new weight each day, one food
becomes a dozen "different" foods and the frequency query can never surface
it. These helpers reduce a label to the generic food it denotes
(``Greek yogurt``) and keep the specific wording as a detail line
(``Chobani nonfat``).

The split is intentionally lossy for grouping and conservative for identity:
amounts, brands, packaging, preparation and marketing adjectives disappear
from the key, while the food's head noun(s) survive. Macros always come from
the latest occurrence, so a folded chip still carries numbers the user can
check and edit in the review sheet.

Public API:
    canonicalize(name) -> (generic_label, detail)
    food_key(name)     -> stable grouping key
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------- cleaning
_PAREN = re.compile(r"\([^()]*\)|\[[^\]]*\]")
_FRACTION = re.compile(r"\b\d+\s*/\s*\d+\b")
_PERCENT = re.compile(r"\b\d+(?:[.,]\d+)?\s*%")
_UNITS = (
    r"(?:g|grams?|kg|mg|ml|l|liters?|litres?|oz|ounces?|lb|lbs|pounds?|cups?|"
    r"tbsp|tsp|teaspoons?|tablespoons?|servings?|slices?|pieces?|cans?|"
    r"pouches?|packages?|packs?|packets?|containers?|bottles?|bars?|scoops?|"
    r"handfuls?|palmfuls?|shots?|units?)"
)
_AMOUNT = re.compile(rf"\b\d+(?:[.,]\d+)?\s*{_UNITS}\b", re.I)
_ANY_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?\b")

# Removed before tokenising so multi-word brands and diet phrases disappear
# cleanly. Longest first; boundaries are checked so "fage" cannot eat "safage".
_PHRASE_DROP = (
    "king's hawaiian", "kings hawaiian", "rise and puff", "rise & puff",
    "tasty bite", "sun cruiser", "jell-o", "joyjello", "reese's", "reeses",
    "flintstones", "chobani", "nancy's", "nancys", "quaker", "kodiak", "fage",
    "whole milk", "whole-milk", "milkfat", "fat free", "fat-free", "low fat",
    "low-fat", "full fat", "full-fat", "nonfat", "non-fat", "protein rich",
    "protein-rich",
    "air-fried", "air fried", "airfried", "single serve", "single-serve",
    "snack size", "snack-size", "snack pack",
)

# Preparation, packaging, and marketing words. Nutritionally meaningful words
# such as "fried", "smoked", "ground", "sweet", "milk", and "greek" are kept:
# they distinguish foods rather than describe how the photo was taken.
_DROP = {
    # preparation
    "cooked", "uncooked", "raw", "fresh", "frozen", "thawed", "steamed",
    "boiled", "baked", "roasted", "grilled", "sauteed", "sautéed", "toasted",
    "reheated", "homemade", "prepared", "ready", "storebought",
    # marketing / diet
    "organic", "probiotic", "plain", "wild", "mixed", "triple", "assorted",
    "natural", "classic", "original", "premium", "gourmet", "complete",
    "chewable", "gummy", "light", "lite", "unsweetened", "sweetened",
    "seasoned", "salt", "salted", "unsalted", "pepper", "blanched",
    # form / packaging / quantities
    "packaged", "pack", "packet", "container", "bottle", "can", "pouch",
    "slice", "sliced", "chopped", "diced", "shredded", "shelled", "shell",
    "pieces", "piece", "chunks", "chunk", "whole", "serving", "servings",
    "side", "small", "medium", "large", "topping", "round", "rounds",
    # glue words that leak out of descriptions
    "in", "of", "with", "and", "a", "an", "the", "on", "for", "to", "my",
    "style", "flavored", "flavour", "flavoured",
}

# If dropping descriptors leaves only a form word ("tablet", "drink"), the
# brand was the identity; keep the cleaned original instead of a useless label.
_FORM_WORDS = {
    "plate", "tablet", "gummy", "drink", "shot", "can", "bar", "candy",
    "cookie", "cracker", "chip", "powder", "supplement", "shake", "smoothie",
    "packet", "pack", "pouch", "bottle", "piece", "slice", "chunk", "roll",
}

_IRREGULAR = {
    "berries": "berry", "leaves": "leaf", "potatoes": "potato",
    "tomatoes": "tomato", "heroes": "hero", "cookies": "cookie",
    "brownies": "brownie", "smoothies": "smoothie", "veggies": "veggie",
    "fries": "fry", "pies": "pie", "smarties": "smarties",
    "madras": "madras",
    "blueberries": "blueberry", "blackberries": "blackberry",
    "raspberries": "raspberry", "strawberries": "strawberry",
    "cranberries": "cranberry",
}

# Head nouns that read naturally in the plural on a chip.
_PLURAL = {
    "berry": "berries", "nut": "nuts", "oat": "oats", "husk": "husks",
    "egg": "eggs", "seed": "seeds", "bean": "beans", "pea": "peas",
    "lentil": "lentils", "leaf": "leaves", "potato": "potatoes",
    "tomato": "tomatoes", "fry": "fries", "roll": "rolls", "cookie": "cookies",
    "carrot": "carrots", "chickpea": "chickpeas", "peanut": "peanuts",
    "almond": "almonds", "noodle": "noodles", "cucumber": "cucumbers",
    "onion": "onions", "olive": "olives", "mushroom": "mushrooms",
}

# Words that describe the specific product but not the food. Excluded from a
# derived detail because "cooked" adds nothing to "Steel cut oats".
_DETAIL_NOISE = {
    "cooked", "uncooked", "raw", "fresh", "frozen", "thawed", "steamed",
    "boiled", "baked", "roasted", "grilled", "sauteed", "sautéed", "toasted",
    "reheated", "homemade", "prepared", "ready", "air", "fried", "packaged",
    "pack", "packet", "container", "bottle", "can", "pouch", "slice",
    "sliced", "chopped", "diced", "shredded", "shelled", "shell", "pieces",
    "piece", "chunks", "chunk", "serving", "servings", "side", "small",
    "medium", "large", "whole", "wild", "mixed", "triple", "topping",
    "round", "rounds", "plain", "protein", "rich", "complete",
    "chewable", "gummy", "original", "premium", "gourmet", "classic",
    "natural", "light", "lite", "seasoned", "salt", "salted", "unsalted",
    "in", "of", "with", "and", "the", "on", "for", "to", "my",
    "slices", "sliced", "chunks", "chunk", "rounds", "round", "pieces",
    "piece", "shell", "shelled", "topping",
}


def _singular(word: str) -> str:
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("oes"):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith(
            ("ss", "us", "is")):
        return word[:-1]
    return word


# Fat level is kept in a readable label ("Greek full fat yogurt") but folded
# out of the grouping key so nonfat / whole-milk / unspecified variants are one
# suggestion. The specific level survives in the detail line either way.
_KEY_DROP = {"full", "fat", "fullfat"}

# Berry varieties fold to a single head noun so a mixed bowl is one suggestion.
_SYNONYM = {
    "oatmeal": "oat",
    "kiwifruit": "kiwi",
    "yoghurt": "yogurt",
    "blueberry": "berry",
    "blackberry": "berry",
    "raspberry": "berry",
    "strawberry": "berry",
    "cranberry": "berry",
    "boysenberry": "berry",
    "gooseberry": "berry",
    "elderberry": "berry",
}


def _fold(name: str) -> str:
    """Casefold and strip accents so "sautéed" tokenises as "sauteed"."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.casefold()


def _clean(name: str) -> str:
    """Lowercase, quantity-free wording used for grouping and labels."""
    s = _fold(name)
    s = s.replace("&", " and ")
    s = s.replace("½", " ").replace("¼", " ").replace("¾", " ")
    while _PAREN.search(s):                 # labels can nest "(1 photo(s))"
        s = _PAREN.sub(" ", s)
    s = _FRACTION.sub(" ", s)
    s = _PERCENT.sub(" ", s)
    s = _AMOUNT.sub(" ", s)
    s = _ANY_NUMBER.sub(" ", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for phrase in sorted(_PHRASE_DROP, key=len, reverse=True):
        s = re.sub(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _specifics(name: str) -> str:
    """Wording with brands and diet words kept, but amounts/parens removed."""
    s = _fold(name)
    s = s.replace("&", " and ")
    s = s.replace("½", " ").replace("¼", " ").replace("¾", " ")
    while _PAREN.search(s):
        s = _PAREN.sub(" ", s)
    s = _FRACTION.sub(" ", s)
    s = _PERCENT.sub(" ", s)
    s = _AMOUNT.sub(" ", s)
    s = _ANY_NUMBER.sub(" ", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(cleaned: str) -> list[str]:
    out: list[str] = []
    for raw in cleaned.split():
        word = raw.strip("'")
        if not word or word.isdigit():
            continue
        word = _singular(word)
        if word in _DROP:
            continue
        word = _SYNONYM.get(word, word)
        if word in _DROP:
            continue
        out.append(word)
    return out


def _display_original(name: str) -> str:
    """The readable original with amounts stripped, for identity-only items."""
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    while _PAREN.search(s):
        s = _PAREN.sub(" ", s)
    s = _FRACTION.sub(" ", s)
    s = _PERCENT.sub(" ", s)
    s = _AMOUNT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;-")
    return s[:1].upper() + s[1:] if s else ""


def _label(tokens: list[str]) -> str:
    words: list[str] = []
    for token in tokens:
        if token not in words:
            words.append(token)
    if not words:
        return ""
    head = words[-1]
    words[-1] = _PLURAL.get(head, head)
    label = " ".join(words)
    return label[:1].upper() + label[1:]


def _detail(name: str, label_tokens: list[str]) -> str:
    label_set = set(label_tokens)
    words = [w.strip("'") for w in _specifics(name).split()]
    rest: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        # "whole milk" is a fat level, not the filler word "whole" -- keep the
        # pair for dairy (but not for "whole psyllium husks").
        if word == "whole" and i + 1 < len(words) and words[i + 1] == "milk":
            if "milk" not in label_set and "whole" not in label_set:
                rest.extend(["whole", "milk"])
            i += 2
            continue
        i += 1
        if not word or word.isdigit() or word in _DETAIL_NOISE:
            continue
        normal = _SYNONYM.get(_singular(word), _singular(word))
        if normal in label_set:
            continue
        rest.append(word)
    if not rest:
        return ""
    detail = " ".join(rest)
    return detail[:1].upper() + detail[1:]


def canonicalize(name: str) -> tuple[str, str]:
    """Return ``(generic_label, detail)`` for a model/user food name.

    ``detail`` is what the label leaves out when it is genuinely specific:
    brand, fat level, or marketing wording. Pure preparation words are
    omitted, so ``cooked steel cut oats`` yields no detail at all.
    """
    cleaned = _clean(name)
    tokens = _tokens(cleaned)
    unique = set(tokens)
    if not tokens or unique <= _FORM_WORDS:
        return _display_original(name) or "Food", ""
    label = _label(tokens) or _display_original(name) or "Food"
    return label, _detail(name, tokens)


def food_key(name: str) -> str:
    """Stable key: the generic head nouns, order-independent."""
    cleaned = _clean(name)
    tokens = [t for t in _tokens(cleaned) if t not in _KEY_DROP]
    if not tokens:
        return cleaned or "food"
    return " ".join(sorted(set(tokens)))
