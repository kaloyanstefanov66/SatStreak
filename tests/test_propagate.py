"""Tests for propagation.

These use a real, historical ISS element set and check against values computed
for a known time and place. The point is not to re-test SGP4, which skyfield and
the sgp4 library already test, but to pin down that elements, observer and time
are being wired together the right way round — a sign error or a swapped
latitude would still produce plausible-looking angles.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from satstreak.catalog import TleRecord
from satstreak.propagate import STALE_ELEMENT_AGE, LookAngles, Propagator
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


def sofia(when: datetime = EPOCH, exposure_s: float | None = None) -> Observation:
    return Observation(
        timestamp=when,
        latitude_deg=42.6977,
        longitude_deg=23.3219,
        elevation_m=550.0,
        exposure_s=exposure_s,
    )


@pytest.fixture(scope="module")
def propagator() -> Propagator:
    return Propagator([ISS, HST])


def test_propagator_indexes_by_norad_id(propagator: Propagator) -> None:
    assert len(propagator) == 2
    assert set(propagator.norad_ids) == {25544, 20580}
    assert propagator.name_of(25544) == "ISS (ZARYA)"


def test_look_angles_are_physically_plausible(propagator: Propagator) -> None:
    angles = propagator.look_angles(25544, sofia())
    assert -90.0 <= angles.altitude_deg <= 90.0
    assert 0.0 <= angles.azimuth_deg < 360.0
    # The ISS orbits at ~420 km, so its range from a ground site runs from about
    # that overhead to ~2500 km at the horizon, and further still below it.
    assert 300.0 < angles.range_km < 6000.0


def test_the_iss_is_below_the_horizon_from_sofia_at_this_epoch(
    propagator: Propagator,
) -> None:
    # Independently computed: at 2024-01-01T12:00Z the ISS sits about 8 degrees
    # below Sofia's horizon. Pinning a known-negative case catches an observer
    # that has been wired in with a flipped sign, which would otherwise pass
    # every "is it plausible" check.
    angles = propagator.look_angles(25544, sofia())
    assert angles.altitude_deg == pytest.approx(-7.99, abs=0.5)
    assert not angles.is_above_horizon


def test_a_different_site_sees_a_different_sky(propagator: Propagator) -> None:
    # Guards against the observer being ignored entirely, which a single-site
    # test cannot detect.
    antipode = Observation(timestamp=EPOCH, latitude_deg=-42.6977, longitude_deg=-156.6781)
    here = propagator.look_angles(25544, sofia())
    there = propagator.look_angles(25544, antipode)
    assert abs(here.altitude_deg - there.altitude_deg) > 10.0


def test_separation_is_spherical_not_a_naive_azimuth_difference() -> None:
    # Ten degrees of azimuth near the zenith is a much smaller angle than ten
    # degrees near the horizon. Subtracting azimuths would report both as 10.
    near_horizon_a = LookAngles(altitude_deg=5.0, azimuth_deg=0.0, range_km=1000.0)
    near_horizon_b = LookAngles(altitude_deg=5.0, azimuth_deg=10.0, range_km=1000.0)
    near_zenith_a = LookAngles(altitude_deg=85.0, azimuth_deg=0.0, range_km=1000.0)
    near_zenith_b = LookAngles(altitude_deg=85.0, azimuth_deg=10.0, range_km=1000.0)

    wide = near_horizon_a.separation_deg(near_horizon_b)
    narrow = near_zenith_a.separation_deg(near_zenith_b)
    assert wide == pytest.approx(9.96, abs=0.1)
    assert narrow == pytest.approx(0.87, abs=0.1)
    assert narrow < wide


def test_separation_of_a_direction_with_itself_is_zero() -> None:
    angles = LookAngles(altitude_deg=33.0, azimuth_deg=140.0, range_km=900.0)
    assert angles.separation_deg(angles) == pytest.approx(0.0, abs=1e-6)


def test_a_track_samples_across_the_exposure(propagator: Propagator) -> None:
    track = propagator.track(25544, sofia(exposure_s=20.0), samples=5)
    assert len(track.samples) == 5
    # Apparent angular rate depends strongly on range: the often-quoted ~0.8 deg/s
    # for the ISS is the overhead case. At this epoch it is ~3400 km away below
    # Sofia's horizon, where the same orbital speed subtends far less angle, and a
    # 20 second exposure sweeps about 0.95 degrees. The assertion is that the
    # samples genuinely move, not that they move at the headline rate.
    assert 0.5 < track.arc_deg < 2.0


def test_a_track_without_an_exposure_collapses_to_one_sample(
    propagator: Propagator,
) -> None:
    # Without knowing how long the shutter was open there is no honest way to say
    # how far anything moved, so the track does not pretend to an arc.
    track = propagator.track(25544, sofia(exposure_s=None), samples=5)
    assert len(track.samples) == 1
    assert track.arc_deg == 0.0


def test_a_longer_exposure_gives_a_longer_arc(propagator: Propagator) -> None:
    short = propagator.track(25544, sofia(exposure_s=5.0))
    long = propagator.track(25544, sofia(exposure_s=30.0))
    assert long.arc_deg > short.arc_deg


def test_stale_elements_are_flagged(propagator: Propagator) -> None:
    fresh = propagator.track(25544, sofia(EPOCH, exposure_s=10.0))
    assert not fresh.elements_are_stale

    much_later = EPOCH + STALE_ELEMENT_AGE + timedelta(days=1)
    stale = propagator.track(25544, sofia(much_later, exposure_s=10.0))
    assert stale.elements_are_stale
    assert stale.element_age > STALE_ELEMENT_AGE


def test_above_horizon_applies_the_altitude_cut(propagator: Propagator) -> None:
    observation = sofia()
    generous = propagator.above_horizon(observation, min_altitude_deg=-90.0)
    strict = propagator.above_horizon(observation, min_altitude_deg=80.0)
    assert set(generous) == {25544, 20580}
    assert strict == []


def test_above_horizon_can_be_restricted_to_a_subset(propagator: Propagator) -> None:
    only_iss = propagator.above_horizon(sofia(), min_altitude_deg=-90.0, norad_ids=[25544])
    assert only_iss == [25544]


def test_track_rejects_a_nonsensical_sample_count(propagator: Propagator) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        propagator.track(25544, sofia(exposure_s=10.0), samples=0)
