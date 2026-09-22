"""The loop closed: real orbit, generated frame, recovered answer.

This is the test the project rests on before real photographs exist. It takes a
genuine ISS pass over Sofia computed from real orbital elements, renders the
frame that pass would have produced, and checks that the matcher names the ISS.

Everything except the camera and the stars is real, and nothing here needs plate
solving: the pointing is an input rather than something recovered. That is the
point. It isolates the geometry and the matcher, so that when a real photograph
later fails, the failure can be attributed to detection or to the solve rather
than to the maths underneath them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from satstreak.catalog import TleRecord
from satstreak.geometry import Pointing
from satstreak.match import match_streak
from satstreak.predict import predict
from satstreak.propagate import Propagator
from satstreak.synthetic import render_frame
from satstreak.types import FindingStatus, Observation, Streak

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

#: A real high pass of the ISS over Sofia, found by scanning the day after the
#: elements' epoch: 77.5 degrees altitude, 430 km range, sweeping 19.5 degrees
#: of sky during a 20 second exposure.
PASS_TIME = datetime(2024, 1, 2, 11, 4, tzinfo=timezone.utc)
ISS_NORAD = 25544


@pytest.fixture(scope="module")
def propagator() -> Propagator:
    return Propagator([ISS, HST])


@pytest.fixture
def observation() -> Observation:
    return Observation(
        timestamp=PASS_TIME,
        latitude_deg=42.6977,
        longitude_deg=23.3219,
        elevation_m=550.0,
        exposure_s=20.0,
    )


@pytest.fixture
def pointing(propagator: Propagator, observation: Observation) -> Pointing:
    """A 60 degree frame aimed where the ISS actually was, wide enough to hold
    the whole 19.5 degree arc."""
    angles = propagator.look_angles(ISS_NORAD, observation)
    width, height = 1600, 1200
    return Pointing(
        altitude_deg=angles.altitude_deg,
        azimuth_deg=angles.azimuth_deg,
        roll_deg=17.0,  # not axis-aligned, so a frame-orientation bug would show
        scale_arcsec_per_px=60.0 * 3600.0 / width,
        width_px=width,
        height_px=height,
    )


def test_a_real_pass_renders_a_trail_with_known_endpoints(
    propagator: Propagator, observation: Observation, pointing: Pointing
) -> None:
    predictions = predict(
        observation, pointing, propagator=propagator, min_altitude_deg=10.0, samples=9
    )
    assert [p.norad_id for p in predictions] == [ISS_NORAD], "only the ISS is up and in frame"

    frame = render_frame(
        observation, pointing, [p.pixels for p in predictions if p.pixels is not None]
    )
    assert frame.image.shape == (1200, 1600)
    assert len(frame.truth) == 1

    trail = frame.truth[0]
    assert trail.norad_id == ISS_NORAD
    # A 19.5 degree arc across a 60 degree, 1600px frame is several hundred pixels.
    assert trail.length_px > 300.0


def test_the_matcher_recovers_the_satellite_that_made_the_trail(
    propagator: Propagator, observation: Observation, pointing: Pointing
) -> None:
    predictions = predict(
        observation, pointing, propagator=propagator, min_altitude_deg=10.0, samples=9
    )
    frame = render_frame(
        observation, pointing, [p.pixels for p in predictions if p.pixels is not None]
    )
    trail = frame.truth[0]

    # Stands in for the detector, which milestone 4 will provide. Using the truth
    # here is deliberate: it isolates the matcher from detection error.
    observed = Streak(x1=trail.x1, y1=trail.y1, x2=trail.x2, y2=trail.y2, detection_score=0.95)

    finding = match_streak(observed, predictions, pointing)

    assert finding.status is FindingStatus.MATCH
    assert finding.best is not None
    assert finding.best.norad_id == ISS_NORAD
    assert finding.best.name == "ISS (ZARYA)"
    assert finding.best.score > 0.8
    assert finding.best.direction_error_deg == pytest.approx(0.0, abs=1.0)
    assert finding.best.length_ratio == pytest.approx(1.0, abs=0.05)


def test_a_trail_at_the_wrong_angle_is_not_forced_onto_the_satellite(
    propagator: Propagator, observation: Observation, pointing: Pointing
) -> None:
    # The ISS is genuinely in frame, so a matcher that simply picked the only
    # candidate would pass the test above while being useless. A trail crossing
    # at a different angle must come back unidentified: an aircraft in the same
    # patch of sky is not the ISS.
    predictions = predict(
        observation, pointing, propagator=propagator, min_altitude_deg=10.0, samples=9
    )
    frame = render_frame(
        observation, pointing, [p.pixels for p in predictions if p.pixels is not None]
    )
    trail = frame.truth[0]

    import math

    centre_x = (trail.x1 + trail.x2) / 2.0
    centre_y = (trail.y1 + trail.y2) / 2.0
    half = trail.length_px / 2.0
    rotated = math.radians(trail.angle_deg + 40.0)
    crossing = Streak(
        x1=centre_x - half * math.cos(rotated),
        y1=centre_y - half * math.sin(rotated),
        x2=centre_x + half * math.cos(rotated),
        y2=centre_y + half * math.sin(rotated),
        detection_score=0.9,
    )

    finding = match_streak(crossing, predictions, pointing)
    assert finding.status is FindingStatus.UNIDENTIFIED
    assert finding.candidates == ()
    assert "aircraft" in finding.message


def test_a_trail_far_from_the_predicted_path_is_not_matched(
    propagator: Propagator, observation: Observation, pointing: Pointing
) -> None:
    # Right direction, wrong place. Without the offset term this would score as
    # well as the real trail.
    predictions = predict(
        observation, pointing, propagator=propagator, min_altitude_deg=10.0, samples=9
    )
    frame = render_frame(
        observation, pointing, [p.pixels for p in predictions if p.pixels is not None]
    )
    trail = frame.truth[0]

    displaced = Streak(
        x1=trail.x1,
        y1=min(trail.y1 + 500.0, 1199.0),
        x2=trail.x2,
        y2=min(trail.y2 + 500.0, 1199.0),
        detection_score=0.9,
    )
    finding = match_streak(displaced, predictions, pointing)
    assert finding.status is FindingStatus.UNIDENTIFIED


def test_an_empty_sky_renders_without_trails(observation: Observation, pointing: Pointing) -> None:
    frame = render_frame(observation, pointing, [])
    assert frame.truth == ()
    assert frame.image.max() > 0, "stars and background are still drawn"


def test_generated_frames_are_reproducible(
    propagator: Propagator, observation: Observation, pointing: Pointing
) -> None:
    # An evaluation that cannot be re-run on the same inputs is not an
    # evaluation, so rendering is seeded.
    predictions = predict(observation, pointing, propagator=propagator, min_altitude_deg=10.0)
    tracks = [p.pixels for p in predictions if p.pixels is not None]
    first = render_frame(observation, pointing, tracks, seed=7)
    second = render_frame(observation, pointing, tracks, seed=7)
    different = render_frame(observation, pointing, tracks, seed=8)

    assert (first.image == second.image).all()
    assert (first.image != different.image).any()
