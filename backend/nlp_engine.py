"""
nlp_engine.py — NLP food categorization for donor broadcasts.

A donor types one raw sentence ("We have 15 kg of cooked paneer left over")
and this module extracts structured tags before the batch is saved:

    {
      "food_type":     "Vegetarian" | "Eggitarian" | "Non-Vegetarian",
      "dietary_tags":  ["vegetarian"] | ["eggitarian"] | ["non-vegetarian"],
      "quantity_kg":   15.0 | None,
      "quantity_source": "explicit_weight" | "estimated_from_count" | "missing",
      "perishable":    True | False,
      "food_items":    ["paneer", ...],
      "confidence":    0.0 – 1.0,
      "engine":        "spacy:en_core_web_sm" | "regex-fallback",
    }

VEGETARIAN-FIRST POLICY
-----------------------
The classifier only labels a batch non-vegetarian on an EXPLICIT meat term.
Anything ambiguous defaults to "vegetarian" — the same default the platform
has always used — and faux-meat phrases ("soya chicken", "mock meat",
"veg keema") are guarded so they stay vegetarian. Perishability defaults to
True: treating unknown food as perishable is the food-safe assumption.

spaCy (en_core_web_sm) is used for tokenization, lemmas, and written-number
detection. If the model or library is unavailable the module degrades to a
pure-regex parser, so the API never fails to boot over an NLP dependency.
"""

import re

try:
    import spacy
except ImportError:  # pragma: no cover — spaCy is in requirements.txt
    spacy = None

_SPACY_MODEL = "en_core_web_sm"
_nlp = None
_engine_name = None


def _get_nlp():
    """Lazy-load spaCy once per process. Falls back to a blank English
    pipeline (tokenizer only) and finally to None (regex-only mode)."""
    global _nlp, _engine_name
    if _engine_name is not None:
        return _nlp
    if spacy is not None:
        try:
            _nlp = spacy.load(_SPACY_MODEL, disable=["parser", "ner"])
            _engine_name = f"spacy:{_SPACY_MODEL}"
            return _nlp
        except OSError:
            _nlp = spacy.blank("en")
            _engine_name = "spacy:blank-en"
            return _nlp
    _nlp = None
    _engine_name = "regex-fallback"
    return _nlp


# ---------------------------------------------------------------------------
# Domain lexicons
# ---------------------------------------------------------------------------

# Explicit animal-flesh terms. Word-boundary matched; faux-meat prefixes are
# stripped from the text before this scan so "soya chicken" never triggers.
_NON_VEG_TERMS = (
    "chicken", "mutton", "lamb", "beef", "pork", "fish", "prawn", "prawns",
    "shrimp", "crab", "meat", "keema", "qeema", "seekh kabab", "seekh kebab",
    "non-veg", "non veg", "nonveg",
)
_FAUX_MEAT_PREFIXES = re.compile(
    r"\b(?:soya|soy|mock|veg|vegan|plant[- ]based)[- ]+"
    r"(?=chicken|mutton|meat|keema|fish|prawn)",
    re.IGNORECASE,
)
_EGG_TERMS = ("egg", "eggs", "omelette", "omelet", "anda", "bhurji")

_PERISHABLE_TERMS = (
    "cooked", "curry", "gravy", "sabzi", "sabji", "dal", "daal", "paneer",
    "milk", "curd", "yogurt", "dahi", "cream", "kheer", "halwa", "sweets",
    "mithai", "salad", "cut fruit", "sandwich", "biryani", "pulao", "khichdi",
    "idli", "dosa", "sambar", "poha", "upma", "roti", "chapati", "naan",
    "paratha", "bread", "leftover", "leftovers", "fresh", "hot", "buffet",
    "thali", "meal", "meals", "rice",
)
_NON_PERISHABLE_TERMS = (
    "packaged", "sealed", "canned", "tinned", "dry", "dried", "raw",
    "uncooked", "biscuits", "cookies", "grain", "grains", "flour", "atta",
    "pulses", "lentils", "namkeen", "chips", "ration", "cereal", "packets of",
)

# Canonical food items surfaced back to the client (vegetarian-heavy lexicon).
_FOOD_LEXICON = (
    "paneer", "dal", "daal", "rice", "roti", "chapati", "naan", "paratha",
    "biryani", "pulao", "khichdi", "sabzi", "sabji", "curry", "idli", "dosa",
    "sambar", "poha", "upma", "thali", "salad", "fruits", "fruit",
    "vegetables", "bread", "sandwich", "kheer", "halwa", "sweets", "mithai",
    "milk", "curd", "yogurt", "dahi", "biscuits", "samosa", "kachori",
    "chole", "rajma", "chicken", "mutton", "fish", "egg", "eggs",
)

# Unit → estimated kilograms per unit. Count-based units (plates, servings)
# use food-bank portion estimates; explicit weight units are exact.
_WEIGHT_UNITS = {
    "kg": 1.0, "kgs": 1.0, "kilo": 1.0, "kilos": 1.0,
    "kilogram": 1.0, "kilograms": 1.0,
    "g": 0.001, "gm": 0.001, "gms": 0.001, "gram": 0.001, "grams": 0.001,
    "quintal": 100.0, "quintals": 100.0,
    "ton": 1000.0, "tons": 1000.0, "tonne": 1000.0, "tonnes": 1000.0,
    "l": 1.0, "litre": 1.0, "litres": 1.0, "liter": 1.0, "liters": 1.0,
}
_COUNT_UNITS = {
    "plate": 0.35, "plates": 0.35, "serving": 0.35, "servings": 0.35,
    "meal": 0.35, "meals": 0.35, "portion": 0.35, "portions": 0.35,
    "person": 0.35, "persons": 0.35, "people": 0.35,
    "box": 2.0, "boxes": 2.0, "tray": 2.0, "trays": 2.0,
    "carton": 2.0, "cartons": 2.0,
    "packet": 0.5, "packets": 0.5, "pack": 0.5, "packs": 0.5,
    "tiffin": 0.75, "tiffins": 0.75, "dozen": 1.5,
}
_ALL_UNITS = {**_WEIGHT_UNITS, **_COUNT_UNITS}

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "half": 0.5,
}

_QUANTITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + "|".join(sorted(_ALL_UNITS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _contains_term(text, terms):
    return [term for term in terms if re.search(rf"\b{re.escape(term)}\b", text)]


def _extract_quantity(text, doc):
    """Best quantity estimate in kg. Explicit weight units beat count-based
    estimates. spaCy catches written numbers ("fifteen kg") the regex misses."""
    candidates = []  # (kg, is_explicit_weight)

    for match in _QUANTITY_RE.finditer(text):
        value, unit = float(match.group(1)), match.group(2).lower()
        candidates.append((value * _ALL_UNITS[unit], unit in _WEIGHT_UNITS))

    if doc is not None:
        tokens = list(doc)
        for index, token in enumerate(tokens[:-1]):
            word = token.text.lower()
            value = None
            if token.like_num:
                try:
                    value = float(word)
                except ValueError:
                    value = _WORD_NUMBERS.get(word)
            elif word in _WORD_NUMBERS:
                value = _WORD_NUMBERS[word]
            if value is None:
                continue
            unit = tokens[index + 1].text.lower()
            if unit in _ALL_UNITS:
                candidates.append((value * _ALL_UNITS[unit], unit in _WEIGHT_UNITS))

    if not candidates:
        return None, "missing"
    explicit = [kg for kg, is_weight in candidates if is_weight]
    if explicit:
        return round(max(explicit), 2), "explicit_weight"
    return round(max(kg for kg, _ in candidates), 2), "estimated_from_count"


def parse_food_description(text):
    """Parse one raw donor sentence into structured food tags. Never raises —
    on any input it returns the vegetarian-first, perishable-first defaults."""
    text = (text or "").strip()
    doc = _get_nlp()(text) if _get_nlp() is not None and text else None
    # Fuse the prefix into the meat word ("soya chicken" -> "vegchicken") so
    # the word-boundary scan below can no longer see a standalone meat term.
    lowered = _FAUX_MEAT_PREFIXES.sub("veg", text.lower())

    non_veg_hits = _contains_term(lowered, _NON_VEG_TERMS)
    egg_hits = _contains_term(lowered, _EGG_TERMS)
    if non_veg_hits:
        food_type, dietary_tags = "Non-Vegetarian", ["non-vegetarian"]
    elif egg_hits:
        # Egg is outside strict vegetarian routing: only accepts_non_veg
        # NGOs match, exactly like meat, but the label stays distinct.
        food_type, dietary_tags = "Eggitarian", ["eggitarian"]
    else:
        food_type, dietary_tags = "Vegetarian", ["vegetarian"]

    perishable_hits = _contains_term(lowered, _PERISHABLE_TERMS)
    non_perishable_hits = _contains_term(lowered, _NON_PERISHABLE_TERMS)
    perishable = not (non_perishable_hits and not perishable_hits)

    quantity_kg, quantity_source = _extract_quantity(lowered, doc)

    food_items = sorted(set(_contains_term(lowered, _FOOD_LEXICON)))

    confidence = 0.5
    if quantity_kg is not None:
        confidence += 0.2
    if food_items:
        confidence += 0.15
    if non_veg_hits or egg_hits or "veg" in lowered:
        confidence += 0.13
    confidence = round(min(confidence, 0.98), 2)

    return {
        "food_type": food_type,
        "dietary_tags": dietary_tags,
        "quantity_kg": quantity_kg,
        "quantity_source": quantity_source,
        "perishable": perishable,
        "food_items": food_items,
        "confidence": confidence,
        "engine": _engine_name,
    }


if __name__ == "__main__":
    samples = [
        "We have 15 kg of cooked paneer left over",
        "Around fifty plates of veg biryani from a wedding reception",
        "3 boxes of sealed biscuit packets and dry ration",
        "Leftover chicken curry, about 8 kg, from hotel dinner service",
        "twenty kg soya chicken chaap, freshly cooked",
        "2 dozen eggs and bread from our canteen",
    ]
    for sentence in samples:
        print(f"\n> {sentence}")
        print(parse_food_description(sentence))
