"""Name resolution and apparent-coordinate computation via Skyfield.

Resolves a target name like "Jupiter", "M31", "Andromeda", or "Vega" into
J2000 ICRS coordinates, then converts to apparent RA/Dec at the observer
location and time. The mount expects "of date" coordinates accounting for
precession, nutation, and aberration. Returning J2000 raw catalog positions
would give consistent pointing offsets that look like calibration bugs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_EPHEMERIS = "de421.bsp"
DEFAULT_LOADER_DIR = Path("~/mira/ephemeris").expanduser()


class NameNotFoundError(LookupError):
    """Raised when a target name cannot be resolved."""


@dataclass(frozen=True)
class TargetCoords:
    """RA/Dec apparent at a specific observer location and time, all in degrees."""

    name: str
    ra_deg: float
    dec_deg: float
    kind: str  # "solar_system", "star", "messier", "ngc", "ic"


# Skyfield names for solar-system bodies in de421.bsp.
SOLAR_SYSTEM_ALIASES: dict[str, str] = {
    "sun": "sun",
    "mercury": "mercury",
    "venus": "venus",
    "moon": "moon",
    "luna": "moon",
    "mars": "mars",
    "jupiter": "jupiter barycenter",
    "saturn": "saturn barycenter",
    "uranus": "uranus barycenter",
    "neptune": "neptune barycenter",
    "pluto": "pluto barycenter",
}


# Bright named stars. RA in hours (J2000 ICRS), Dec in degrees.
# Sourced from the IAU-approved star names list and Hipparcos catalog.
NAMED_STARS: dict[str, tuple[float, float]] = {
    "sirius":      (6.752477, -16.716116),
    "canopus":     (6.399195, -52.695661),
    "rigil kentaurus": (14.660765, -60.833975),
    "alpha centauri":  (14.660765, -60.833975),
    "toliman":     (14.660765, -60.833975),
    "arcturus":    (14.261027, 19.182410),
    "vega":        (18.615649, 38.783689),
    "capella":     (5.278155,  45.997991),
    "rigel":       (5.242297, -8.201638),
    "procyon":     (7.655033,  5.224993),
    "achernar":    (1.628573, -57.236757),
    "betelgeuse":  (5.919529,  7.407064),
    "hadar":       (14.063723, -60.373035),
    "altair":      (19.846388, 8.868321),
    "acrux":       (12.443311, -63.099092),
    "aldebaran":   (4.598677,  16.509302),
    "antares":     (16.490128, -26.432002),
    "spica":       (13.419883, -11.161319),
    "pollux":      (7.755263,  28.026198),
    "fomalhaut":   (22.960846, -29.622236),
    "deneb":       (20.690532, 45.280339),
    "mimosa":      (12.795357, -59.688767),
    "regulus":     (10.139532, 11.967209),
    "adhara":      (6.977098, -28.972085),
    "shaula":      (17.560145, -37.103823),
    "castor":      (7.576634,  31.888276),
    "gacrux":      (12.519429, -57.113213),
    "bellatrix":   (5.418852,  6.349703),
    "elnath":      (5.438198,  28.607450),
    "miaplacidus": (9.220069, -69.717208),
    "alnilam":     (5.603559, -1.201920),
    "alnitak":     (5.679313, -1.942572),
    "alnair":      (22.137209, -46.960975),
    "alioth":      (12.900475, 55.959823),
    "mirfak":      (3.405380,  49.861179),
    "dubhe":       (11.062130, 61.751035),
    "wezen":       (7.139857, -26.393200),
    "kaus australis": (18.402866, -34.384616),
    "alkaid":      (13.792354, 49.313265),
    "sargas":      (17.621981, -42.997824),
    "menkalinan":  (5.992149,  44.947433),
    "atria":       (16.811079, -69.027712),
    "alhena":      (6.628528,  16.399280),
    "peacock":     (20.427456, -56.735090),
    "polaris":     (2.530302,  89.264109),
    "mirzam":      (6.378330, -17.955918),
    "alphard":     (9.459789, -8.658603),
    "hamal":       (2.119557,  23.462418),
    "diphda":      (0.726486, -17.986605),
    "nunki":       (18.921094, -26.296725),
    "mira":        (2.322422, -2.977630),
    "saiph":       (5.795941, -9.669606),
    "rasalhague":  (17.582243, 12.560037),
    "kochab":      (14.845104, 74.155505),
    "denebola":    (11.817664, 14.572058),
    "algieba":     (10.332877, 19.841489),
    "naos":        (8.060800, -40.003148),
    "izar":        (14.749785, 27.074222),
    "alphecca":    (15.578131, 26.714693),
    "menkar":      (3.037994,  4.089735),
    "merak":       (11.030672, 56.382428),
    "schedar":     (0.675122,  56.537331),
    "alpheratz":   (0.139792,  29.090432),
    "caph":        (0.152888,  59.149780),
    "phecda":      (11.897181, 53.694758),
    "navi":        (0.945142,  60.716738),
    "ruchbah":     (1.430218,  60.235284),
    "albireo":     (19.512030, 27.959693),
    "alcor":       (13.420446, 54.987854),
    "mizar":       (13.398765, 54.925360),
    "thuban":      (14.073171, 64.375851),
    "etamin":      (17.943437, 51.488896),
}


# Messier catalog. RA in hours (J2000 ICRS), Dec in degrees.
# Source: SIMBAD/MESSIER catalog, rounded to standard precision.
MESSIER_CATALOG: dict[int, tuple[float, float]] = {
    1:  (5.575556, 22.014722),
    2:  (21.557505, -0.823250),
    3:  (13.703283, 28.377275),
    4:  (16.393119, -26.525750),
    5:  (15.309228, 2.081028),
    6:  (17.668333, -32.246667),
    7:  (17.897500, -34.793333),
    8:  (18.060500, -24.380000),
    9:  (17.319719, -18.516389),
    10: (16.952517, -4.099944),
    11: (18.851000, -6.273333),
    12: (16.787262, -1.948528),
    13: (16.694900, 36.461319),
    14: (17.626706, -3.245833),
    15: (21.499533, 12.167000),
    16: (18.312500, -13.778889),
    17: (18.346167, -16.171667),
    18: (18.331167, -17.103333),
    19: (17.043555, -26.267944),
    20: (18.045167, -22.971667),
    21: (18.071667, -22.500000),
    22: (18.606658, -23.904750),
    23: (17.940500, -19.014444),
    24: (18.281667, -18.550000),
    25: (18.529833, -19.246667),
    26: (18.752833, -9.388333),
    27: (19.993431, 22.721336),
    28: (18.409142, -24.870111),
    29: (20.398167, 38.522222),
    30: (21.672833, -23.179000),
    31: (0.712314, 41.268750),
    32: (0.711619, 40.865167),
    33: (1.564139, 30.660194),
    34: (2.701667, 42.762778),
    35: (6.150000, 24.336111),
    36: (5.605000, 34.135000),
    37: (5.871667, 32.553333),
    38: (5.477500, 35.831389),
    39: (21.531667, 48.433333),
    40: (12.371667, 58.083333),
    41: (6.781667, -20.756667),
    42: (5.591500, -5.391111),
    43: (5.594528, -5.270000),
    44: (8.668000, 19.621667),
    45: (3.790833, 24.105000),
    46: (7.696667, -14.808889),
    47: (7.609722, -14.487500),
    48: (8.230667, -5.798889),
    49: (12.496333, 7.999722),
    50: (7.052500, -8.337778),
    51: (13.497972, 47.195258),
    52: (23.413500, 61.593333),
    53: (13.215344, 18.168167),
    54: (18.917500, -30.479861),
    55: (19.666631, -30.964756),
    56: (19.276683, 30.183444),
    57: (18.893083, 33.029194),
    58: (12.628194, 11.818083),
    59: (12.700583, 11.646944),
    60: (12.727750, 11.552750),
    61: (12.365433, 4.473672),
    62: (17.020167, -30.112361),
    63: (13.263717, 42.029289),
    64: (12.945458, 21.682639),
    65: (11.315528, 13.092250),
    66: (11.337500, 12.991667),
    67: (8.855000, 11.813889),
    68: (12.657805, -26.744056),
    69: (18.523083, -32.348083),
    70: (18.720200, -32.292111),
    71: (19.896200, 18.779194),
    72: (20.891267, -12.537306),
    73: (20.984167, -12.625278),
    74: (1.611591, 15.783644),
    75: (20.101344, -21.922258),
    76: (1.705500, 51.575167),
    77: (2.711308, -0.013294),
    78: (5.779278, 0.078889),
    79: (5.404069, -24.524278),
    80: (16.284167, -22.976250),
    81: (9.925881, 69.065295),
    82: (9.931231, 69.679703),
    83: (13.616923, -29.865761),
    84: (12.418336, 12.886983),
    85: (12.422758, 18.191353),
    86: (12.436531, 12.946181),
    87: (12.513728, 12.391122),
    88: (12.532133, 14.420578),
    89: (12.594386, 12.556353),
    90: (12.613831, 13.162922),
    91: (12.590667, 14.496389),
    92: (17.285389, 43.135944),
    93: (7.742500, -23.857222),
    94: (12.848083, 41.120508),
    95: (10.732722, 11.703694),
    96: (10.779389, 11.819944),
    97: (11.246908, 55.019028),
    98: (12.230083, 14.900472),
    99: (12.313792, 14.416469),
    100: (12.381925, 15.822306),
    101: (14.053492, 54.348750),
    102: (15.108167, 55.763056),
    103: (1.555167, 60.658056),
    104: (12.666508, -11.623053),
    105: (10.797125, 12.581636),
    106: (12.316002, 47.303719),
    107: (16.542183, -13.053778),
    108: (11.191803, 55.674139),
    109: (11.959999, 53.374664),
    110: (0.672794, 41.685419),
}


# Common-name aliases for popular Messier objects and other DSOs.
DSO_ALIASES: dict[str, str] = {
    "andromeda":         "M31",
    "andromeda galaxy":  "M31",
    "triangulum":        "M33",
    "triangulum galaxy": "M33",
    "pleiades":          "M45",
    "seven sisters":     "M45",
    "orion nebula":      "M42",
    "orion":             "M42",
    "lagoon":            "M8",
    "lagoon nebula":     "M8",
    "trifid":            "M20",
    "trifid nebula":     "M20",
    "eagle nebula":      "M16",
    "swan nebula":       "M17",
    "omega nebula":      "M17",
    "ring nebula":       "M57",
    "dumbbell":          "M27",
    "dumbbell nebula":   "M27",
    "crab":              "M1",
    "crab nebula":       "M1",
    "sombrero":          "M104",
    "sombrero galaxy":   "M104",
    "whirlpool":         "M51",
    "whirlpool galaxy":  "M51",
    "pinwheel":          "M101",
    "pinwheel galaxy":   "M101",
    "bode":              "M81",
    "bodes galaxy":      "M81",
    "cigar":             "M82",
    "cigar galaxy":      "M82",
    "sunflower":         "M63",
    "sunflower galaxy":  "M63",
    "owl":               "M97",
    "owl nebula":        "M97",
    "great cluster in hercules": "M13",
    "hercules cluster":  "M13",
    "beehive":           "M44",
    "beehive cluster":   "M44",
    "praesepe":          "M44",
    "ptolemy":           "M7",
    "butterfly cluster": "M6",
    "wild duck":         "M11",
    "wild duck cluster": "M11",
}


def _normalize(name: str) -> str:
    return name.strip().lower().replace("-", " ")


def _parse_messier(name: str) -> Optional[int]:
    """Return Messier number for inputs like 'M31', 'm 31', 'Messier 31'."""
    n = _normalize(name).replace(".", "")
    n = n.replace("messier", "m").replace(" ", "")
    if n.startswith("m") and n[1:].isdigit():
        num = int(n[1:])
        if 1 <= num <= 110:
            return num
    return None


class Ephemeris:
    """Wraps Skyfield. Holds the loader so we don't redownload ephemeris files."""

    def __init__(
        self,
        observer_lat_deg: float,
        observer_lon_deg: float,
        elevation_m: float = 0.0,
        loader_dir: Path | None = None,
        ephemeris_file: str = DEFAULT_EPHEMERIS,
    ) -> None:
        from skyfield.api import Loader, wgs84

        self._loader_dir = (loader_dir or DEFAULT_LOADER_DIR).expanduser()
        self._loader_dir.mkdir(parents=True, exist_ok=True)
        self._loader = Loader(str(self._loader_dir))
        self._ts = self._loader.timescale()
        self._eph = self._loader(ephemeris_file)
        self._earth = self._eph["earth"]
        self._site = wgs84.latlon(observer_lat_deg, observer_lon_deg, elevation_m=elevation_m)
        self._observer = self._earth + self._site

    def resolve(self, name: str, when: datetime | None = None) -> TargetCoords:
        """Resolve a target name to apparent RA/Dec at the observer's location.

        Args:
            name: target name. Supports solar-system bodies, M1-M110,
                  named stars, and common DSO aliases.
            when: UTC datetime to compute apparent position. Defaults to now.

        Returns:
            TargetCoords with apparent RA/Dec in degrees.

        Raises:
            NameNotFoundError: if the name does not match any known target.
        """
        when = when or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        t = self._ts.from_datetime(when)
        norm = _normalize(name)

        # Solar-system bodies first.
        if norm in SOLAR_SYSTEM_ALIASES:
            target = self._eph[SOLAR_SYSTEM_ALIASES[norm]]
            astrometric = self._observer.at(t).observe(target)
            apparent = astrometric.apparent()
            ra, dec, _ = apparent.radec(epoch="date")
            return TargetCoords(
                name=name,
                ra_deg=float(ra._degrees),
                dec_deg=float(dec.degrees),
                kind="solar_system",
            )

        # DSO common-name aliases redirect into Messier.
        if norm in DSO_ALIASES:
            return self.resolve(DSO_ALIASES[norm], when=when)

        # Messier numeric.
        m_num = _parse_messier(name)
        if m_num is not None and m_num in MESSIER_CATALOG:
            ra_h, dec_d = MESSIER_CATALOG[m_num]
            return self._apparent_from_j2000(name=f"M{m_num}", ra_hours=ra_h, dec_deg=dec_d, t=t, kind="messier")

        # Named stars.
        if norm in NAMED_STARS:
            ra_h, dec_d = NAMED_STARS[norm]
            return self._apparent_from_j2000(name=name, ra_hours=ra_h, dec_deg=dec_d, t=t, kind="star")

        raise NameNotFoundError(
            f"could not resolve target name {name!r}. "
            "Known: solar-system bodies, M1-M110, named bright stars, common DSO aliases."
        )

    def _apparent_from_j2000(
        self,
        name: str,
        ra_hours: float,
        dec_deg: float,
        t,
        kind: str,
    ) -> TargetCoords:
        from skyfield.api import Star

        star = Star(ra_hours=ra_hours, dec_degrees=dec_deg)
        astrometric = self._observer.at(t).observe(star)
        apparent = astrometric.apparent()
        ra, dec, _ = apparent.radec(epoch="date")
        return TargetCoords(
            name=name,
            ra_deg=float(ra._degrees),
            dec_deg=float(dec.degrees),
            kind=kind,
        )


@lru_cache(maxsize=4)
def get_ephemeris(
    observer_lat_deg: float,
    observer_lon_deg: float,
    elevation_m: float = 0.0,
    loader_dir: str | None = None,
) -> Ephemeris:
    """Cached factory so we don't reload de421.bsp on every call."""
    return Ephemeris(
        observer_lat_deg=observer_lat_deg,
        observer_lon_deg=observer_lon_deg,
        elevation_m=elevation_m,
        loader_dir=Path(loader_dir) if loader_dir else None,
    )


def list_known_names() -> dict[str, list[str]]:
    """Return a categorized list of all names this resolver understands."""
    return {
        "solar_system": sorted(set(SOLAR_SYSTEM_ALIASES.keys())),
        "named_stars": sorted(NAMED_STARS.keys()),
        "messier": [f"M{n}" for n in sorted(MESSIER_CATALOG.keys())],
        "dso_aliases": sorted(DSO_ALIASES.keys()),
    }
