"""Tests for the plate-solving layer.

The backend itself needs Linux or WSL and half a gigabyte of index files, so it
is not exercised here. What is tested is everything around it: the conversion
from a solve's equatorial answer into this project's horizontal frame, the star
extraction fed to it, and the pipeline's behaviour when a solve fails — which
must be a clear refusal rather than a guess.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pytest

from satstreak.geometry import Pointing
from satstreak.propagate import Propagator
from satstreak.scan import scan_image
from satstreak.solve import AstrometryNetSolver, SolveResult, equatorial_to_pointing
from satstreak.stars import Star, stars_above_horizon
from satstreak.synthetic import _stamp_gaussian, draw_trail
from satstreak.types import ImageStatus, Observation

SOFIA_LAT = 42.6977
SOFIA_LON = 23.3219
WHEN = datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc)


def observation() -> Observation:
    return Observation(
        timestamp=WHEN,
        latitude_deg=SOFIA_LAT,
        longitude_deg=SOFIA_LON,
        elevation_m=550.0,
        exposure_s=20.0,
    )


# --- equatorial to horizontal ------------------------------------------------


def test_a_solve_at_the_pole_becomes_a_pointing_due_north() -> None:
    # The same geometric anchor used for stars: a solve centred on the celestial
    # pole must come back as due north at the observer's latitude.
    pointing = equatorial_to_pointing(
        ra_deg=0.0,
        dec_deg=90.0,
        scale_arcsec_per_px=100.0,
        orientation_deg=0.0,
        observation=observation(),
        width_px=1600,
        height_px=1200,
    )
    assert pointing.altitude_deg == pytest.approx(SOFIA_LAT, abs=1e-6)
    assert pointing.azimuth_deg == pytest.approx(0.0, abs=1e-6)


def test_the_conversion_agrees_with_the_star_catalogue_route() -> None:
    # Two independent paths to the same answer: a star converted by stars.py and
    # a solve centred on that star converted by solve.py. Disagreement would mean
    # the solver's output lands in a different frame from the satellites it is
    # compared against, which is the kind of error that produces plausible
    # nonsense rather than an obvious failure.
    star = Star(hip=91262, ra_deg=279.2347, dec_deg=38.7837, magnitude=0.03)  # Vega
    ((_, angles),) = stars_above_horizon([star], observation(), min_altitude_deg=-90.0)

    pointing = equatorial_to_pointing(
        ra_deg=star.ra_deg,
        dec_deg=star.dec_deg,
        scale_arcsec_per_px=100.0,
        orientation_deg=0.0,
        observation=observation(),
        width_px=1600,
        height_px=1200,
    )
    assert pointing.altitude_deg == pytest.approx(angles.altitude_deg, abs=1e-6)
    assert pointing.azimuth_deg == pytest.approx(angles.azimuth_deg, abs=1e-6)


def test_the_plate_scale_is_carried_through() -> None:
    pointing = equatorial_to_pointing(
        ra_deg=100.0,
        dec_deg=20.0,
        scale_arcsec_per_px=112.5,
        orientation_deg=0.0,
        observation=observation(),
        width_px=1600,
        height_px=1200,
    )
    assert pointing.scale_arcsec_per_px == pytest.approx(112.5)
    assert pointing.field_width_deg == pytest.approx(50.0, abs=0.01)


# --- star extraction ---------------------------------------------------------


def field_with(stars: int = 60, trail: bool = False) -> np.ndarray:
    rng = np.random.default_rng(4)
    image = np.full((600, 800), 12.0, dtype=np.float32)
    for _ in range(stars):
        _stamp_gaussian(
            image,
            float(rng.uniform(20, 780)),
            float(rng.uniform(20, 580)),
            float(rng.uniform(40, 200)),
            1.4,
        )
    if trail:
        draw_trail(image, [(50.0, 100.0), (750.0, 480.0)], 120.0, 1.4)
    image += rng.normal(0, 3.0, image.shape).astype(np.float32)
    return image


def test_extraction_finds_the_stars() -> None:
    solver = AstrometryNetSolver(cache_directory="unused")
    found = solver.extract_stars(field_with(stars=60))
    assert 40 <= len(found) <= 60
    assert all(len(point) == 2 for point in found)


def test_extraction_rejects_the_satellite_trail() -> None:
    # Handing a trail to the solver as though it were a star corrupts the
    # geometry being recovered, so the elongation filter must drop it.
    solver = AstrometryNetSolver(cache_directory="unused")
    without = solver.extract_stars(field_with(stars=60, trail=False))
    with_trail = solver.extract_stars(field_with(stars=60, trail=True))
    assert abs(len(with_trail) - len(without)) <= 2


def test_extraction_is_capped_at_the_requested_count() -> None:
    # Quad matching is combinatorial in the number of sources, so the cap is
    # what keeps a dense field from taking minutes.
    solver = AstrometryNetSolver(cache_directory="unused", max_stars=25)
    assert len(solver.extract_stars(field_with(stars=200))) <= 25


def test_extraction_of_an_empty_frame_returns_nothing() -> None:
    rng = np.random.default_rng(1)
    noise = (12.0 + rng.normal(0, 3.0, (400, 400))).astype(np.float32)
    solver = AstrometryNetSolver(cache_directory="unused")
    assert solver.extract_stars(noise) == []


# --- what the pipeline does when solving fails -------------------------------


class FailingSolver:
    """Stands in for a frame that cannot be solved: too few stars, cloud, a
    photograph of something other than the sky."""

    def solve(self, image, observation, fov_hint_deg=None) -> SolveResult:
        return SolveResult(
            pointing=None, seconds=0.4, stars_used=3, message="Only 3 star-like sources."
        )


def test_a_failed_solve_with_no_fallback_refuses_to_guess() -> None:
    # The whole point of keeping NO_POINTING as its own outcome: not knowing
    # where the camera looked is different from finding nothing in the sky.
    image = field_with(stars=60, trail=True)
    result = scan_image(image, observation(), solver=FailingSolver(), propagator=Propagator([]))

    assert result.status is ImageStatus.NO_POINTING
    assert result.findings == ()
    assert "Could not work out where the camera was aimed" in result.message
    assert result.diagnostics["plate_solve"]["solved"] is False
    assert result.diagnostics["plate_solve"]["stars_used"] == 3


def test_a_failed_solve_falls_back_to_a_supplied_pointing() -> None:
    # A user who knows roughly where they aimed should not be blocked by the
    # solver failing.
    image = field_with(stars=60, trail=True)
    fallback = Pointing(
        altitude_deg=70.0,
        azimuth_deg=180.0,
        roll_deg=0.0,
        scale_arcsec_per_px=112.5,
        width_px=800,
        height_px=600,
    )
    result = scan_image(
        image, observation(), fallback, solver=FailingSolver(), propagator=Propagator([])
    )
    assert result.status is ImageStatus.FOUND
    assert result.diagnostics["plate_solve"]["solved"] is False


def test_scanning_without_a_pointing_or_a_solver_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="either a pointing or a solver"):
        scan_image(field_with(), observation())


def test_a_successful_solve_supplies_the_pointing() -> None:
    aimed = Pointing(
        altitude_deg=64.0,
        azimuth_deg=210.0,
        roll_deg=0.0,
        scale_arcsec_per_px=112.5,
        width_px=800,
        height_px=600,
    )

    class WorkingSolver:
        def solve(self, image, observation, fov_hint_deg=None) -> SolveResult:
            return SolveResult(pointing=aimed, seconds=1.2, stars_used=88, message="ok")

    result = scan_image(
        field_with(stars=60, trail=True),
        observation(),
        solver=WorkingSolver(),
        propagator=Propagator([]),
    )
    assert result.status is ImageStatus.FOUND
    assert result.diagnostics["plate_solve"]["solved"] is True
    assert result.diagnostics["pointing"]["altitude_deg"] == pytest.approx(64.0)
    assert math.isfinite(result.diagnostics["pointing"]["field_width_deg"])
