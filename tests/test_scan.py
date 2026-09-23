"""The product, end to end: a frame goes in, named satellites come out.

Runs the real pipeline — detect, predict, match — over a frame rendered from a
genuine ISS pass. The only thing standing in for reality is the camera, and the
propagator is injected so nothing here touches the network or depends on which
satellites happen to be in orbit today.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from satstreak.catalog import TleRecord
from satstreak.geometry import Pointing
from satstreak.predict import predict
from satstreak.propagate import Propagator
from satstreak.scan import scan_image
from satstreak.synthetic import SyntheticFrame, render_frame
from satstreak.types import FindingStatus, ImageStatus, Observation

ISS = TleRecord(
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24001.50000000  .00016717  00000-0  30777-3 0  9993",
    line2="2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49447149 10000",
)
PASS_TIME = datetime(2024, 1, 2, 11, 4, tzinfo=timezone.utc)
ISS_NORAD = 25544


@pytest.fixture(scope="module")
def propagator() -> Propagator:
    return Propagator([ISS])


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
    angles = propagator.look_angles(ISS_NORAD, observation)
    width, height = 1200, 900
    return Pointing(
        altitude_deg=angles.altitude_deg,
        azimuth_deg=angles.azimuth_deg,
        roll_deg=11.0,
        scale_arcsec_per_px=60.0 * 3600.0 / width,
        width_px=width,
        height_px=height,
    )


@pytest.fixture
def frame(propagator: Propagator, observation: Observation, pointing: Pointing) -> SyntheticFrame:
    predictions = predict(observation, pointing, propagator=propagator, samples=9)
    return render_frame(
        observation,
        pointing,
        [p.pixels for p in predictions if p.pixels is not None],
        seed=5,
    )


def test_the_pipeline_names_the_satellite_that_made_the_trail(
    frame: SyntheticFrame,
    observation: Observation,
    pointing: Pointing,
    propagator: Propagator,
) -> None:
    # Detection, prediction and matching together, with nothing told where to
    # look. This is the product working.
    result = scan_image(frame.image, observation, pointing, propagator=propagator)

    assert result.status is ImageStatus.FOUND
    assert len(result.findings) == 1

    finding = result.findings[0]
    assert finding.status is FindingStatus.MATCH
    assert finding.best is not None
    assert finding.best.norad_id == ISS_NORAD
    assert finding.best.name == "ISS (ZARYA)"
    assert finding.best.score > 0.7


def test_the_detected_trail_matches_where_the_satellite_really_was(
    frame: SyntheticFrame,
    observation: Observation,
    pointing: Pointing,
    propagator: Propagator,
) -> None:
    # Detection error accumulates into the match, so it is worth pinning
    # separately: the detector must recover the truth geometry closely.
    result = scan_image(frame.image, observation, pointing, propagator=propagator)
    detected = result.findings[0].streak
    truth = frame.truth[0]

    assert detected.length_px == pytest.approx(truth.length_px, rel=0.05)
    difference = abs(detected.angle_deg - truth.angle_deg) % 180.0
    assert min(difference, 180.0 - difference) < 1.0


def test_an_empty_sky_is_reported_as_empty_not_as_a_failure(
    observation: Observation, pointing: Pointing, propagator: Propagator
) -> None:
    rng = np.random.default_rng(2)
    noise = np.clip(
        12.0 + rng.normal(0, 3.0, (pointing.height_px, pointing.width_px)), 0, 255
    ).astype(np.uint8)

    result = scan_image(noise, observation, pointing, propagator=propagator)
    assert result.status is ImageStatus.NO_STREAKS
    assert result.findings == ()
    assert "No satellite trails" in result.message


def test_diagnostics_record_how_the_answer_was_reached(
    frame: SyntheticFrame,
    observation: Observation,
    pointing: Pointing,
    propagator: Propagator,
) -> None:
    # Without these, a wrong answer cannot be attributed to detection, to the
    # candidate list, or to scoring.
    result = scan_image(frame.image, observation, pointing, propagator=propagator)
    diagnostics = result.diagnostics

    assert diagnostics["streaks_detected"] == 1
    assert diagnostics["candidates_in_frame"] >= 1
    assert diagnostics["pointing"]["field_width_deg"] == pytest.approx(60.0, abs=0.1)
    assert "threshold_sigma" in diagnostics["detector"]
    assert diagnostics["observation"]["exposure_s"] == 20.0


def test_a_trail_with_no_candidate_overhead_is_reported_unidentified(
    frame: SyntheticFrame, observation: Observation, pointing: Pointing
) -> None:
    # An empty catalogue stands in for an aircraft or an uncatalogued object:
    # the trail is real and is reported, but nothing is named. A tool that
    # volunteers findings must be able to say "something, but I do not know what".
    empty = Propagator([])
    result = scan_image(frame.image, observation, pointing, propagator=empty)

    assert result.status is ImageStatus.FOUND
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.status is FindingStatus.UNIDENTIFIED
    assert finding.candidates == ()
    assert result.matched == ()


# --- solving for sensor roll ------------------------------------------------


def test_roll_is_recovered_without_being_told(
    frame: SyntheticFrame,
    observation: Observation,
    pointing: Pointing,
    propagator: Propagator,
) -> None:
    # Roll is the one pointing value a photographer cannot report, so the tool
    # has to find it. The frame was rendered at 11 degrees; the scan is given 0.
    from dataclasses import replace

    wrong = replace(pointing, roll_deg=0.0)
    result = scan_image(frame.image, observation, wrong, propagator=propagator, search_roll=True)

    assert result.diagnostics["roll_searched"] is True
    assert result.diagnostics["roll_deg"] == pytest.approx(11.0, abs=1.0)
    assert result.findings[0].status is FindingStatus.MATCH
    assert result.findings[0].best is not None
    assert result.findings[0].best.norad_id == ISS_NORAD


def test_without_the_roll_search_a_wrong_roll_finds_nothing(
    frame: SyntheticFrame,
    observation: Observation,
    pointing: Pointing,
    propagator: Propagator,
) -> None:
    # The companion to the test above: it shows the search is doing real work
    # rather than the match being insensitive to rotation.
    from dataclasses import replace

    wrong = replace(pointing, roll_deg=0.0)
    result = scan_image(frame.image, observation, wrong, propagator=propagator)
    assert result.findings[0].status is FindingStatus.UNIDENTIFIED


def test_the_roll_search_reports_how_well_it_did(
    frame: SyntheticFrame,
    observation: Observation,
    pointing: Pointing,
    propagator: Propagator,
) -> None:
    # A search always returns its best angle, so the score it reached is what
    # distinguishes a real fit from the least bad of a bad set.
    from dataclasses import replace

    result = scan_image(
        frame.image,
        observation,
        replace(pointing, roll_deg=0.0),
        propagator=propagator,
        search_roll=True,
    )
    assert result.diagnostics["roll_search_total_score"] > 0.5


def test_roll_is_recovered_in_the_second_half_of_a_turn(
    propagator: Propagator, observation: Observation, pointing: Pointing
) -> None:
    # A trail's own angle wraps at 180 because a trail is an axis, not an arrow,
    # which makes it tempting to search only half a turn. That is wrong: rolling
    # the sensor by 180 degrees sends every pixel to the opposite side of the
    # frame, so roll 200 and roll 20 place the same track in different places.
    # Searching half a turn left every answer above 180 unreachable, and this
    # case is the one that catches it.
    from dataclasses import replace

    from satstreak.predict import predict
    from satstreak.synthetic import render_frame

    rolled = replace(pointing, roll_deg=200.0)
    predictions = predict(observation, rolled, propagator=propagator, samples=9)
    frame = render_frame(
        observation, rolled, [p.pixels for p in predictions if p.pixels is not None], seed=5
    )

    result = scan_image(
        frame.image,
        observation,
        replace(rolled, roll_deg=0.0),
        propagator=propagator,
        search_roll=True,
    )
    assert result.diagnostics["roll_deg"] == pytest.approx(200.0, abs=1.0)
    assert result.findings[0].status is FindingStatus.MATCH
    assert result.findings[0].best.norad_id == ISS_NORAD
