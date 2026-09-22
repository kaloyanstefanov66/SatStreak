"""Tests for prediction.

Network-free: every test injects a propagator built from fixed elements, so CI
never downloads the catalogue and the expected answers never drift as real
satellites are launched and deorbited.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from satstreak.catalog import TleRecord
from satstreak.geometry import Pointing
from satstreak.predict import predict
from satstreak.propagate import Propagator
from satstreak.types import Observation

ISS = TleRecord(
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24001.50000000  .00016717  00000-0  30777-3 0  9993",
    line2="2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49447149 10000",
)
HST = TleRecord(
    name="HST",
    line1="1 20580U 90037B   24001.48752314  .00001262  00000-0  64592-4 0  9993",
    line2="2 20580  28.4696 288.8102 0002708 328.0797 194.8871 15.10309691 10001",
)

EPOCH = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def propagator() -> Propagator:
    return Propagator([ISS, HST])


def observation(when: datetime = EPOCH, exposure_s: float | None = 20.0) -> Observation:
    return Observation(
        timestamp=when,
        latitude_deg=42.6977,
        longitude_deg=23.3219,
        elevation_m=550.0,
        exposure_s=exposure_s,
    )


def test_nothing_is_predicted_when_nothing_is_up(propagator: Propagator) -> None:
    # Both fixtures are below Sofia's horizon at this epoch, and an empty list is
    # the correct answer rather than a failure.
    assert predict(observation(), propagator=propagator) == []


def test_a_generous_altitude_cut_returns_what_is_in_the_sky(propagator: Propagator) -> None:
    results = predict(observation(), propagator=propagator, min_altitude_deg=-90.0)
    assert {p.norad_id for p in results} == {25544, 20580}
    assert all(p.pixels is None for p in results), "no pointing given, so no pixel paths"


def test_results_are_ordered_by_peak_altitude(propagator: Propagator) -> None:
    results = predict(observation(), propagator=propagator, min_altitude_deg=-90.0)
    altitudes = [p.peak_altitude_deg for p in results]
    assert altitudes == sorted(altitudes, reverse=True)


def test_a_pointing_restricts_the_result_to_what_crossed_the_frame(
    propagator: Propagator,
) -> None:
    # Aimed at a patch of sky where neither fixture is, so both are filtered out
    # even though the altitude cut would admit them.
    elsewhere = Pointing(
        altitude_deg=80.0,
        azimuth_deg=0.0,
        roll_deg=0.0,
        scale_arcsec_per_px=10.0,
        width_px=1000,
        height_px=1000,
    )
    results = predict(observation(), elsewhere, propagator=propagator, min_altitude_deg=-90.0)
    assert results == []


def test_a_satellite_in_the_frame_gets_a_pixel_path(propagator: Propagator) -> None:
    # Aim directly at where the ISS is, so it must appear with a measured path.
    angles = propagator.look_angles(25544, observation())
    aimed = Pointing(
        altitude_deg=angles.altitude_deg,
        azimuth_deg=angles.azimuth_deg,
        roll_deg=0.0,
        scale_arcsec_per_px=60.0,
        width_px=4000,
        height_px=3000,
    )
    results = predict(observation(), aimed, propagator=propagator, min_altitude_deg=-90.0)

    assert [p.norad_id for p in results] == [25544]
    found = results[0]
    assert found.in_frame
    assert found.pixels is not None
    assert found.pixels.length_px > 0.0
    assert found.name == "ISS (ZARYA)"


def test_a_longer_exposure_draws_a_longer_trail(propagator: Propagator) -> None:
    angles = propagator.look_angles(25544, observation())
    aimed = Pointing(
        altitude_deg=angles.altitude_deg,
        azimuth_deg=angles.azimuth_deg,
        roll_deg=0.0,
        scale_arcsec_per_px=60.0,
        width_px=4000,
        height_px=3000,
    )
    short = predict(
        observation(exposure_s=2.0), aimed, propagator=propagator, min_altitude_deg=-90.0
    )
    long = predict(
        observation(exposure_s=30.0), aimed, propagator=propagator, min_altitude_deg=-90.0
    )
    assert short[0].pixels is not None and long[0].pixels is not None
    assert long[0].pixels.length_px > short[0].pixels.length_px


def test_prediction_exposes_element_staleness(propagator: Propagator) -> None:
    # A prediction from month-old elements is not worth the same as one from
    # today's, and the caller must be able to tell them apart.
    fresh = predict(observation(), propagator=propagator, min_altitude_deg=-90.0)
    assert not fresh[0].track.elements_are_stale

    from datetime import timedelta

    later = predict(
        observation(EPOCH + timedelta(days=30)),
        propagator=propagator,
        min_altitude_deg=-90.0,
    )
    assert later[0].track.elements_are_stale
