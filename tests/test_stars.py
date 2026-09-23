"""Tests for the bright-star catalogue and the horizontal conversion.

The conversion is checked against cases whose answers follow from geometry alone,
rather than against another implementation. A star at the celestial pole must sit
due north at an altitude equal to the observer's latitude, whatever else is true;
if that fails, nothing downstream can be trusted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from satstreak.stars import (
    Star,
    StarCatalogError,
    cache_path,
    load_stars,
    parse_hipparcos,
    stars_above_horizon,
)
from satstreak.types import Observation

SOFIA_LAT = 42.6977
SOFIA_LON = 23.3219
WHEN = datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc)

# Three real rows from the published catalogue, in its pipe-delimited format.
SAMPLE = "\n".join(
    [
        "H|           1| |00 00 00.22|+01 05 20.4| 9.10| |H|000.00091185|+01.08901332| |",
        "H|           3| |00 00 01.20|+38 51 33.4| 6.61| |G|000.00500795|+38.85928608| |",
        "H|       32349| |06 45 08.92|-16 42 58.0|-1.44| |H|101.28715533|-16.71611586| |",
    ]
)


def observation() -> Observation:
    return Observation(
        timestamp=WHEN, latitude_deg=SOFIA_LAT, longitude_deg=SOFIA_LON, elevation_m=550.0
    )


# --- parsing ----------------------------------------------------------------


def test_parsing_keeps_only_stars_within_the_magnitude_limit() -> None:
    stars = parse_hipparcos(SAMPLE, magnitude_limit=6.5)
    assert [s.hip for s in stars] == [32349], "only Sirius is brighter than 6.5"
    assert stars[0].magnitude == pytest.approx(-1.44)
    assert stars[0].ra_deg == pytest.approx(101.2872, abs=1e-3)
    assert stars[0].dec_deg == pytest.approx(-16.7161, abs=1e-3)


def test_parsing_sorts_brightest_first() -> None:
    stars = parse_hipparcos(SAMPLE, magnitude_limit=10.0)
    assert [s.magnitude for s in stars] == sorted(s.magnitude for s in stars)


def test_parsing_skips_rows_without_usable_astrometry() -> None:
    text = SAMPLE + "\nH| garbage |\nnot a row at all"
    assert len(parse_hipparcos(text, magnitude_limit=10.0)) == 3


def test_parsing_rejects_a_response_that_is_not_the_catalogue() -> None:
    # A server error page would otherwise be read as an empty sky.
    with pytest.raises(StarCatalogError, match="not the Hipparcos catalogue"):
        parse_hipparcos("<html>503 Service Unavailable</html>")


def test_magnitudes_convert_to_a_linear_brightness() -> None:
    # Five magnitudes is a factor of a hundred; reading magnitudes literally
    # would render every star the same.
    bright = Star(hip=1, ra_deg=0.0, dec_deg=0.0, magnitude=0.0)
    faint = Star(hip=2, ra_deg=0.0, dec_deg=0.0, magnitude=5.0)
    assert bright.relative_brightness / faint.relative_brightness == pytest.approx(100.0)


# --- caching ----------------------------------------------------------------


def test_the_catalogue_is_fetched_once_then_served_from_cache(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        return SAMPLE

    first = load_stars(10.0, cache_dir=tmp_path, fetcher=fetcher)
    second = load_stars(10.0, cache_dir=tmp_path, fetcher=fetcher)

    assert len(first) == len(second) == 3
    assert len(calls) == 1, "a cached catalogue must not be downloaded again"
    assert cache_path(10.0, tmp_path).exists()


def test_the_cache_round_trips_positions_and_magnitudes(tmp_path: Path) -> None:
    load_stars(10.0, cache_dir=tmp_path, fetcher=lambda url: SAMPLE)
    reloaded = load_stars(10.0, cache_dir=tmp_path, fetcher=lambda url: "")
    sirius = next(s for s in reloaded if s.hip == 32349)
    assert sirius.ra_deg == pytest.approx(101.28716, abs=1e-5)
    assert sirius.magnitude == pytest.approx(-1.44)


# --- the horizontal conversion ----------------------------------------------


def test_a_star_at_the_pole_sits_due_north_at_the_observers_latitude() -> None:
    pole = Star(hip=0, ra_deg=0.0, dec_deg=90.0, magnitude=1.0)
    ((_, angles),) = stars_above_horizon([pole], observation(), min_altitude_deg=-90.0)
    assert angles.altitude_deg == pytest.approx(SOFIA_LAT, abs=1e-6)
    assert angles.azimuth_deg == pytest.approx(0.0, abs=1e-6)


def test_a_star_on_the_meridian_at_the_equator_sits_due_south() -> None:
    from skyfield.api import load

    observed = observation()
    sidereal = (
        load.timescale().from_datetime(observed.utc).gast * 15.0 + observed.longitude_deg
    ) % 360.0
    meridian = Star(hip=1, ra_deg=sidereal, dec_deg=0.0, magnitude=1.0)

    ((_, angles),) = stars_above_horizon([meridian], observed, min_altitude_deg=-90.0)
    assert angles.altitude_deg == pytest.approx(90.0 - SOFIA_LAT, abs=1e-3)
    assert angles.azimuth_deg == pytest.approx(180.0, abs=1e-3)


def test_a_star_at_the_southern_pole_is_below_a_northern_horizon() -> None:
    south = Star(hip=2, ra_deg=0.0, dec_deg=-90.0, magnitude=1.0)
    assert stars_above_horizon([south], observation(), min_altitude_deg=0.0) == []

    ((_, angles),) = stars_above_horizon([south], observation(), min_altitude_deg=-90.0)
    assert angles.altitude_deg == pytest.approx(-SOFIA_LAT, abs=1e-6)


def test_the_altitude_cut_is_applied() -> None:
    pole = Star(hip=0, ra_deg=0.0, dec_deg=90.0, magnitude=1.0)
    assert stars_above_horizon([pole], observation(), min_altitude_deg=80.0) == []
    assert len(stars_above_horizon([pole], observation(), min_altitude_deg=40.0)) == 1


def test_an_empty_catalogue_gives_an_empty_sky() -> None:
    assert stars_above_horizon([], observation()) == []
