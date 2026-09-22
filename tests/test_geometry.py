"""Tests for the sky-to-sensor projection.

The projection is where a sign error does the most damage, because a mirrored or
rotated frame still produces plausible-looking pixel coordinates and would only
show up as a matcher that quietly never works. So the orientation of every axis
is pinned explicitly rather than assumed.
"""

from __future__ import annotations

import math

import pytest

from satstreak.geometry import WIDE_FIELD_THRESHOLD_DEG, Pointing
from satstreak.propagate import LookAngles, Track


def angles(altitude: float, azimuth: float, range_km: float = 800.0) -> LookAngles:
    return LookAngles(altitude_deg=altitude, azimuth_deg=azimuth, range_km=range_km)


@pytest.fixture
def camera() -> Pointing:
    """A 4000x3000 frame aimed due south, 45 degrees up, about 28 degrees wide."""
    return Pointing(
        altitude_deg=45.0,
        azimuth_deg=180.0,
        roll_deg=0.0,
        scale_arcsec_per_px=25.0,
        width_px=4000,
        height_px=3000,
    )


def test_the_aim_point_lands_in_the_middle_of_the_frame(camera: Pointing) -> None:
    point = camera.project(angles(45.0, 180.0))
    assert point is not None
    assert point.x == pytest.approx(2000.0, abs=1e-6)
    assert point.y == pytest.approx(1500.0, abs=1e-6)


def test_higher_in_the_sky_is_higher_in_the_image(camera: Pointing) -> None:
    # Image y increases downwards, so greater altitude must give smaller y. A
    # flipped sign here would mirror every track vertically.
    higher = camera.project(angles(50.0, 180.0))
    assert higher is not None
    assert higher.y < 1500.0
    assert higher.x == pytest.approx(2000.0, abs=1e-6)


def test_azimuth_increases_towards_the_right_of_the_frame(camera: Pointing) -> None:
    # Facing south, increasing azimuth turns west, which appears to the right.
    westward = camera.project(angles(45.0, 185.0))
    assert westward is not None
    assert westward.x > 2000.0


def test_the_field_of_view_matches_the_plate_scale(camera: Pointing) -> None:
    assert camera.field_width_deg == pytest.approx(4000 * 25.0 / 3600.0)
    assert camera.field_height_deg == pytest.approx(3000 * 25.0 / 3600.0)


def test_an_offset_matching_the_plate_scale_lands_where_expected(camera: Pointing) -> None:
    # One degree from centre, along the vertical through the aim point, must be
    # 3600/scale pixels away. This is what ties pixels to angles.
    one_degree_up = camera.project(angles(46.0, 180.0))
    assert one_degree_up is not None
    expected = 3600.0 / 25.0
    assert 1500.0 - one_degree_up.y == pytest.approx(expected, rel=1e-3)


def test_directions_outside_the_frame_are_reported_as_outside(camera: Pointing) -> None:
    # Most of the catalogue is outside any given frame. That is an answer, not
    # an error.
    assert camera.project(angles(45.0, 90.0)) is None
    assert not camera.contains(angles(5.0, 180.0))


def test_a_direction_behind_the_camera_is_not_projected(camera: Pointing) -> None:
    # Without the depth check, a direction behind the lens projects to a
    # plausible point in front of it.
    assert camera.project(angles(-45.0, 0.0)) is None


def test_roll_rotates_the_frame(camera: Pointing) -> None:
    rolled = Pointing(
        altitude_deg=45.0,
        azimuth_deg=180.0,
        roll_deg=90.0,
        scale_arcsec_per_px=25.0,
        width_px=4000,
        height_px=3000,
    )
    # With the sensor turned a quarter turn, what was directly above the aim
    # point moves to the side.
    upright = camera.project(angles(46.0, 180.0))
    turned = rolled.project(angles(46.0, 180.0))
    assert upright is not None and turned is not None
    assert upright.x == pytest.approx(2000.0, abs=1e-3)
    assert turned.y == pytest.approx(1500.0, abs=1e-3)
    assert abs(turned.x - 2000.0) == pytest.approx(abs(upright.y - 1500.0), rel=1e-3)


def test_pointing_at_the_zenith_stays_well_defined() -> None:
    # Azimuth is degenerate at the zenith, which is where a naive basis breaks.
    overhead = Pointing(
        altitude_deg=90.0,
        azimuth_deg=0.0,
        roll_deg=0.0,
        scale_arcsec_per_px=30.0,
        width_px=2000,
        height_px=2000,
    )
    centre = overhead.project(angles(90.0, 0.0))
    assert centre is not None
    assert centre.x == pytest.approx(1000.0, abs=1e-6)
    assert centre.y == pytest.approx(1000.0, abs=1e-6)
    nearby = overhead.project(angles(85.0, 0.0))
    assert nearby is not None


def test_wide_fields_are_flagged() -> None:
    narrow = Pointing(
        altitude_deg=45.0,
        azimuth_deg=0.0,
        roll_deg=0.0,
        scale_arcsec_per_px=10.0,
        width_px=4000,
        height_px=3000,
    )
    assert not narrow.is_wide_field

    # A phone main camera: roughly 70 degrees across, which is where the
    # tangent-plane model and a real lens start to disagree at the corners.
    phone = Pointing(
        altitude_deg=45.0,
        azimuth_deg=0.0,
        roll_deg=0.0,
        scale_arcsec_per_px=70.0 * 3600.0 / 4000.0,
        width_px=4000,
        height_px=3000,
    )
    assert phone.is_wide_field
    assert phone.field_width_deg > WIDE_FIELD_THRESHOLD_DEG


@pytest.mark.parametrize(
    ("field", "value"),
    [("altitude_deg", 91.0), ("scale_arcsec_per_px", 0.0), ("width_px", 0)],
)
def test_pointing_rejects_impossible_values(field: str, value: float) -> None:
    kwargs = {
        "altitude_deg": 45.0,
        "azimuth_deg": 0.0,
        "roll_deg": 0.0,
        "scale_arcsec_per_px": 25.0,
        "width_px": 4000,
        "height_px": 3000,
        field: value,
    }
    with pytest.raises(ValueError):
        Pointing(**kwargs)


# --- tracks -----------------------------------------------------------------


def a_track(samples: tuple[LookAngles, ...]) -> Track:
    from datetime import timedelta

    return Track(norad_id=25544, name="ISS (ZARYA)", samples=samples, element_age=timedelta(0))


def test_a_track_crossing_the_frame_becomes_a_pixel_path(camera: Pointing) -> None:
    track = a_track(tuple(angles(45.0, 178.0 + i * 1.0) for i in range(5)))
    projected = camera.project_track(track)
    assert projected.crosses_frame
    assert len(projected.visible_points) == 5
    assert projected.length_px > 0.0
    # Moving in azimuth at constant altitude draws a roughly horizontal line.
    assert projected.angle_deg == pytest.approx(0.0, abs=2.0)


def test_a_track_that_misses_the_frame_reports_no_crossing(camera: Pointing) -> None:
    track = a_track(tuple(angles(10.0, 10.0 + i) for i in range(5)))
    projected = camera.project_track(track)
    assert not projected.crosses_frame
    assert projected.visible_points == ()
    assert projected.length_px == 0.0
    assert projected.angle_deg is None


def test_a_track_entering_mid_exposure_keeps_its_gaps(camera: Pointing) -> None:
    # Samples outside the frame stay as None rather than being dropped, so the
    # caller can tell where in the exposure the satellite entered.
    track = a_track(tuple(angles(45.0, 150.0 + i * 8.0) for i in range(5)))
    projected = camera.project_track(track)
    assert any(p is None for p in projected.points)
    assert len(projected.points) == 5


def test_the_projected_angle_matches_a_detected_streak_convention(camera: Pointing) -> None:
    # Both wrap at 180 degrees, so a track and the streak it made compare
    # directly without either side worrying about which end came first.
    rising = a_track(tuple(angles(44.0 + i * 0.5, 178.0 + i * 0.5) for i in range(4)))
    projected = camera.project_track(rising)
    assert projected.angle_deg is not None
    assert 0.0 <= projected.angle_deg < 180.0
    assert math.isfinite(projected.angle_deg)
