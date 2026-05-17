"""Classify a target name into one of the exposure preset categories.

Used by goto() to auto-pick the right exposure preset before plate-solving
and capture. Categories match exposure_tuning.PRESETS keys:

    "moon"     -- the Moon
    "planet"   -- solar-system planets
    "cluster"  -- open or globular star clusters
    "nebula"   -- emission / planetary / reflection nebulae
    "galaxy"   -- external galaxies
    "star"     -- bright individual stars
    "default"  -- anything we don't recognize
"""
from __future__ import annotations

import re


# Solar-system planets observable from Earth-based amateur scopes.
PLANETS: set[str] = {
    "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune",
    # Pluto is a dwarf planet but visually behaves like a faint star;
    # bucket it as "star" instead.
}

# A modest set of named bright stars that show up in goto requests. The
# full set of valid star names is large; we only need the names users
# actually type. Anything else falls through to "default".
BRIGHT_STARS: set[str] = {
    "polaris", "vega", "sirius", "arcturus", "rigel", "betelgeuse",
    "capella", "procyon", "altair", "deneb", "antares", "spica",
    "aldebaran", "regulus", "fomalhaut", "pollux", "castor",
    "albireo", "mizar", "alkaid", "dubhe", "merak",
}

# Messier-number -> category lookup. Compiled from the standard catalog
# breakdown. We bucket open and globular clusters together as "cluster"
# because the exposure preset is the same.
MESSIER_TYPES: dict[int, str] = {
    # Nebulae (emission, reflection, planetary, supernova remnant)
    1: "nebula",   16: "nebula",  17: "nebula",  20: "nebula",
    27: "nebula",  42: "nebula",  43: "nebula",  57: "nebula",
    76: "nebula",  78: "nebula",  97: "nebula",

    # Galaxies
    31: "galaxy",  32: "galaxy",  33: "galaxy",  49: "galaxy",
    51: "galaxy",  58: "galaxy",  59: "galaxy",  60: "galaxy",
    61: "galaxy",  63: "galaxy",  64: "galaxy",  65: "galaxy",
    66: "galaxy",  74: "galaxy",  77: "galaxy",  81: "galaxy",
    82: "galaxy",  83: "galaxy",  84: "galaxy",  85: "galaxy",
    86: "galaxy",  87: "galaxy",  88: "galaxy",  89: "galaxy",
    90: "galaxy",  91: "galaxy",  94: "galaxy",  95: "galaxy",
    96: "galaxy",  98: "galaxy",  99: "galaxy", 100: "galaxy",
    101: "galaxy", 102: "galaxy", 104: "galaxy", 105: "galaxy",
    106: "galaxy", 108: "galaxy", 109: "galaxy", 110: "galaxy",

    # Star clusters (open + globular both -> "cluster")
    2: "cluster",   3: "cluster",   4: "cluster",   5: "cluster",
    6: "cluster",   7: "cluster",   9: "cluster",  10: "cluster",
    11: "cluster",  12: "cluster",  13: "cluster",  14: "cluster",
    15: "cluster",  18: "cluster",  19: "cluster",  21: "cluster",
    22: "cluster",  23: "cluster",  25: "cluster",  26: "cluster",
    28: "cluster",  29: "cluster",  30: "cluster",  34: "cluster",
    35: "cluster",  36: "cluster",  37: "cluster",  38: "cluster",
    39: "cluster",  41: "cluster",  44: "cluster",  45: "cluster",
    46: "cluster",  47: "cluster",  48: "cluster",  50: "cluster",
    52: "cluster",  53: "cluster",  54: "cluster",  55: "cluster",
    56: "cluster",  62: "cluster",  67: "cluster",  68: "cluster",
    69: "cluster",  70: "cluster",  71: "cluster",  72: "cluster",
    73: "cluster",  75: "cluster",  79: "cluster",  80: "cluster",
    92: "cluster",  93: "cluster", 103: "cluster", 107: "cluster",

    # M24 and M40 are weird (star cloud and a double star); call them default.
    24: "default", 40: "default",
}

# Common-name aliases -> normalized canonical name. Mirrors ephemeris.DSO_ALIASES
# style but here we map to category directly when the alias doesn't go through
# a Messier number.
COMMON_NAME_TO_CATEGORY: dict[str, str] = {
    # Direct planet aliases
    "the moon": "moon",  "moon": "moon",  "luna": "moon",

    # Notable non-Messier DSOs (NGC objects users might type)
    "double cluster": "cluster",
    "horsehead":      "nebula",
    "rosette":        "nebula",
    "veil nebula":    "nebula",
    "ngc 7000":       "nebula",   # North America Nebula
    "north america":  "nebula",
    "ngc 869":        "cluster",  # half of Double Cluster
    "ngc 884":        "cluster",
}


_MESSIER_RE = re.compile(r"^m\s*0*(\d{1,3})$", re.IGNORECASE)


def _normalize(name: str) -> str:
    """Mirror ephemeris._normalize so we accept the same inputs."""
    return name.strip().lower().replace("-", " ")


def get_target_type(name: str) -> str:
    """Classify `name` into one of the preset categories.

    Returns "default" for anything unrecognized. Never raises -- callers
    should be able to use this to pick a starting exposure for any input
    that ephemeris.resolve() accepted.
    """
    n = _normalize(name)

    if n in COMMON_NAME_TO_CATEGORY:
        return COMMON_NAME_TO_CATEGORY[n]

    if n in PLANETS:
        return "planet"

    if n in BRIGHT_STARS:
        return "star"

    # Messier — bracket Mnnn or M nnn
    m = _MESSIER_RE.match(n)
    if m:
        num = int(m.group(1))
        if num in MESSIER_TYPES:
            return MESSIER_TYPES[num]

    # Last resort: route through ephemeris's DSO alias table to redirect
    # "Pleiades" -> "M45" etc. Late import avoids a circular dep at module load.
    try:
        from .ephemeris import DSO_ALIASES
        canonical = DSO_ALIASES.get(n)
        if canonical:
            m = _MESSIER_RE.match(_normalize(canonical))
            if m:
                num = int(m.group(1))
                if num in MESSIER_TYPES:
                    return MESSIER_TYPES[num]
    except Exception:
        pass

    return "default"


def classify_with_confidence(name: str) -> tuple[str, str]:
    """Returns (category, reason). Reason explains WHY this category, useful
    for the goto narration and for debugging unexpected classifications."""
    n = _normalize(name)

    if n in COMMON_NAME_TO_CATEGORY:
        return COMMON_NAME_TO_CATEGORY[n], f"common name alias for '{n}'"
    if n in PLANETS:
        return "planet", "solar-system planet"
    if n in BRIGHT_STARS:
        return "star", "named bright star"
    m = _MESSIER_RE.match(n)
    if m:
        num = int(m.group(1))
        if num in MESSIER_TYPES:
            return MESSIER_TYPES[num], f"Messier catalog M{num}"
        return "default", f"M{num} not in classification table"

    # Route through ephemeris alias table
    try:
        from .ephemeris import DSO_ALIASES
        canonical = DSO_ALIASES.get(n)
        if canonical:
            m = _MESSIER_RE.match(_normalize(canonical))
            if m:
                num = int(m.group(1))
                if num in MESSIER_TYPES:
                    return MESSIER_TYPES[num], f"DSO alias '{n}' -> {canonical}"
    except Exception:
        pass

    return "default", f"unrecognized name '{n}'"
