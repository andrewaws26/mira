"""Tests for mira.ephemeris: name resolution and apparent coordinate math.

These tests download de421.bsp on first run, which is ~17 MB. After that
the file is cached under ~/mira/ephemeris/ and tests run offline.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mira.ephemeris import (
    DSO_ALIASES,
    MESSIER_CATALOG,
    NAMED_STARS,
    SOLAR_SYSTEM_ALIASES,
    Ephemeris,
    NameNotFoundError,
    _normalize,
    _parse_messier,
    list_known_names,
)


# Fixed time for reproducible apparent-coord computations.
T = datetime(2026, 5, 9, 4, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def eph(tmp_path_factory: pytest.TempPathFactory) -> Ephemeris:
    """Build one ephemeris for the module. Caches de421.bsp under tmp."""
    loader_dir = tmp_path_factory.mktemp("eph")
    return Ephemeris(
        observer_lat_deg=38.2527,
        observer_lon_deg=-85.7585,
        elevation_m=142.0,
        loader_dir=loader_dir,
    )


class TestParsing:
    def test_normalize(self) -> None:
        assert _normalize("  Jupiter  ") == "jupiter"
        assert _normalize("RIGEL") == "rigel"
        assert _normalize("Alpha-Centauri") == "alpha centauri"

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("M31", 31),
            ("m31", 31),
            ("M 31", 31),
            ("messier 31", 31),
            ("Messier 1", 1),
            ("M110", 110),
        ],
    )
    def test_parse_messier_valid(self, name: str, expected: int) -> None:
        assert _parse_messier(name) == expected

    @pytest.mark.parametrize("name", ["M0", "M111", "Mars", "Vega", "M", ""])
    def test_parse_messier_invalid(self, name: str) -> None:
        assert _parse_messier(name) is None


class TestCatalogs:
    def test_messier_complete(self) -> None:
        assert set(MESSIER_CATALOG.keys()) == set(range(1, 111))

    def test_messier_ranges(self) -> None:
        for num, (ra_h, dec_d) in MESSIER_CATALOG.items():
            assert 0 <= ra_h < 24, f"M{num} ra_h out of range: {ra_h}"
            assert -90 <= dec_d <= 90, f"M{num} dec out of range: {dec_d}"

    def test_named_stars_ranges(self) -> None:
        for name, (ra_h, dec_d) in NAMED_STARS.items():
            assert 0 <= ra_h < 24, f"{name} ra_h out of range"
            assert -90 <= dec_d <= 90, f"{name} dec out of range"

    def test_solar_system_includes_planets(self) -> None:
        for body in ["sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn"]:
            assert body in SOLAR_SYSTEM_ALIASES

    def test_dso_aliases_resolve_to_messier(self) -> None:
        for alias, target in DSO_ALIASES.items():
            assert _parse_messier(target) is not None, (
                f"alias {alias!r} -> {target!r} which is not a Messier id"
            )

    def test_list_known_names(self) -> None:
        names = list_known_names()
        assert "solar_system" in names
        assert "messier" in names
        assert "named_stars" in names
        assert "M31" in names["messier"]
        assert "vega" in names["named_stars"]


@pytest.mark.online
class TestResolution:
    def test_jupiter(self, eph: Ephemeris) -> None:
        coords = eph.resolve("Jupiter", when=T)
        assert coords.kind == "solar_system"
        assert 0 <= coords.ra_deg < 360
        assert -90 <= coords.dec_deg <= 90

    def test_moon(self, eph: Ephemeris) -> None:
        coords = eph.resolve("moon", when=T)
        assert coords.kind == "solar_system"

    def test_m31(self, eph: Ephemeris) -> None:
        coords = eph.resolve("M31", when=T)
        assert coords.kind == "messier"
        # M31 is at RA ~10.7 deg, Dec ~41.3 deg J2000.
        # Apparent coords drift a bit but should stay close.
        assert abs(coords.ra_deg - 10.68) < 1.0
        assert abs(coords.dec_deg - 41.27) < 0.5

    def test_andromeda_alias(self, eph: Ephemeris) -> None:
        coords = eph.resolve("Andromeda", when=T)
        assert coords.kind == "messier"
        # Same physical target as M31.
        m31 = eph.resolve("M31", when=T)
        assert abs(coords.ra_deg - m31.ra_deg) < 1e-6
        assert abs(coords.dec_deg - m31.dec_deg) < 1e-6

    def test_vega(self, eph: Ephemeris) -> None:
        coords = eph.resolve("Vega", when=T)
        assert coords.kind == "star"
        # Vega J2000 RA ~279.23 deg, Dec ~38.78 deg.
        assert abs(coords.ra_deg - 279.5) < 2.0
        assert abs(coords.dec_deg - 38.78) < 0.5

    def test_polaris_close_to_pole(self, eph: Ephemeris) -> None:
        coords = eph.resolve("Polaris", when=T)
        assert coords.kind == "star"
        assert coords.dec_deg > 89.0

    def test_unknown_raises(self, eph: Ephemeris) -> None:
        with pytest.raises(NameNotFoundError):
            eph.resolve("Definitely Not A Real Target", when=T)

    def test_case_insensitive(self, eph: Ephemeris) -> None:
        a = eph.resolve("vega", when=T)
        b = eph.resolve("VEGA", when=T)
        c = eph.resolve("Vega", when=T)
        assert a.ra_deg == b.ra_deg == c.ra_deg

    def test_naive_datetime_treated_as_utc(self, eph: Ephemeris) -> None:
        naive = datetime(2026, 5, 9, 4, 0, 0)
        aware = naive.replace(tzinfo=timezone.utc)
        a = eph.resolve("Jupiter", when=naive)
        b = eph.resolve("Jupiter", when=aware)
        assert abs(a.ra_deg - b.ra_deg) < 1e-6
        assert abs(a.dec_deg - b.dec_deg) < 1e-6


@pytest.mark.online
class TestApparentVsJ2000:
    """Apparent coords differ from J2000 catalog values in a measurable but
    bounded way. This proves we are computing apparent, not just returning
    catalog positions raw."""

    def test_m31_drifts_from_j2000(self, eph: Ephemeris) -> None:
        coords = eph.resolve("M31", when=T)
        j2000_ra = MESSIER_CATALOG[31][0] * 15.0  # hours -> degrees
        j2000_dec = MESSIER_CATALOG[31][1]
        # Precession over ~26 years moves M31 by tens of arcseconds at most.
        # Should be nonzero and small.
        delta_ra = coords.ra_deg - j2000_ra
        delta_dec = coords.dec_deg - j2000_dec
        assert delta_ra != 0.0
        assert abs(delta_ra) < 1.0
        assert abs(delta_dec) < 1.0
