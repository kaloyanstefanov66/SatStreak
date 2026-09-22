"""Tests for trail detection.

Most of these are about what the detector *refuses* to report. Because the tool
volunteers findings rather than confirming ones the user already spotted, a false
trail becomes a confident wrong answer that nobody can check, while a missed one
is only a missed one. The asymmetry is deliberate and is tested for directly.
"""

from __future__ import annotations

import numpy as np
import pytest

from satstreak.detect import DetectorSettings, detect_streaks
from satstreak.synthetic import _stamp_gaussian, draw_trail

RNG_SEED = 11


def blank(height: int = 600, width: int = 800, noise: float = 3.0) -> np.ndarray:
    rng = np.random.default_rng(RNG_SEED)
    return (
        np.full((height, width), 12.0, dtype=np.float32) + rng.normal(0, noise, (height, width))
    ).astype(np.float32)


def with_stars(image: np.ndarray, count: int = 200) -> np.ndarray:
    rng = np.random.default_rng(RNG_SEED + 1)
    out = image.copy()
    for _ in range(count):
        _stamp_gaussian(
            out,
            float(rng.uniform(0, out.shape[1])),
            float(rng.uniform(0, out.shape[0])),
            float(rng.pareto(1.8) * 25.0 + 15.0),
            1.4,
        )
    return out


def draw_line(
    image: np.ndarray, x1: float, y1: float, x2: float, y2: float, brightness: float = 45.0
) -> np.ndarray:
    """Draw a trail peaking at `brightness` above the background.

    Uses the library's own renderer so that "brightness" means the same thing
    here as it does in a generated frame.
    """
    out = image.copy()
    draw_trail(out, [(x1, y1), (x2, y2)], brightness, 1.4)
    return out


def test_a_clean_trail_is_found_with_the_right_geometry() -> None:
    image = draw_line(with_stars(blank()), 100.0, 150.0, 600.0, 430.0)
    found = detect_streaks(image)

    assert len(found) == 1
    streak = found[0]
    expected_length = float(np.hypot(600.0 - 100.0, 430.0 - 150.0))
    expected_angle = float(np.degrees(np.arctan2(430.0 - 150.0, 600.0 - 100.0)) % 180.0)
    assert streak.length_px == pytest.approx(expected_length, rel=0.05)
    assert streak.angle_deg == pytest.approx(expected_angle, abs=1.0)


def test_a_field_of_stars_alone_yields_nothing() -> None:
    # The most important negative case. Stars are round; the elongation test that
    # accepts trails must reject every one of them.
    assert detect_streaks(with_stars(blank(), count=400)) == []


def test_pure_noise_yields_nothing() -> None:
    assert detect_streaks(blank(noise=6.0)) == []


def test_several_trails_are_all_reported() -> None:
    # One image can hold more than one trail, which is the whole reason the
    # result type carries a list of findings.
    image = with_stars(blank())
    image = draw_line(image, 50.0, 80.0, 700.0, 120.0)
    image = draw_line(image, 120.0, 500.0, 500.0, 200.0)
    found = detect_streaks(image)
    assert len(found) == 2


def test_a_short_smudge_is_rejected() -> None:
    # Cosmic ray hits and adjacent hot pixels are short and can be elongated.
    # Length is what separates them from a trail.
    image = draw_line(with_stars(blank()), 400.0, 300.0, 412.0, 306.0)
    assert detect_streaks(image) == []


def test_the_length_threshold_can_be_relaxed_deliberately() -> None:
    # The trade between precision and recall must be movable on purpose rather
    # than by accident, so the settings are explicit.
    image = draw_line(with_stars(blank()), 400.0, 300.0, 412.0, 306.0)
    permissive = DetectorSettings(min_length_px=8.0, min_elongation=2.0, min_area_px=6)
    assert len(detect_streaks(image, permissive)) >= 1


def test_a_faint_trail_falls_below_the_threshold() -> None:
    # Honest about the limit: a trail at the noise level is not detected, and the
    # detector says nothing rather than guessing.
    faint = draw_line(with_stars(blank()), 100.0, 150.0, 600.0, 430.0, brightness=4.0)
    assert detect_streaks(faint) == []


def test_brightness_determines_whether_a_trail_is_seen() -> None:
    # The same geometry at two brightnesses, to show the boundary is set by
    # signal and not by shape.
    bright = draw_line(with_stars(blank()), 100.0, 150.0, 600.0, 430.0, brightness=45.0)
    dim = draw_line(with_stars(blank()), 100.0, 150.0, 600.0, 430.0, brightness=6.0)
    assert len(detect_streaks(bright)) == 1
    assert len(detect_streaks(dim)) == 0


def test_endpoints_are_reported_not_just_the_centroid() -> None:
    # The matcher compares endpoints, so a detector that returned only a centre
    # and a direction would silently lose the length term.
    image = draw_line(with_stars(blank()), 150.0, 100.0, 650.0, 100.0)
    streak = detect_streaks(image)[0]
    xs = sorted([streak.x1, streak.x2])
    assert xs[0] == pytest.approx(150.0, abs=15.0)
    assert xs[1] == pytest.approx(650.0, abs=15.0)


def test_a_colour_image_is_rejected_rather_than_guessed_at() -> None:
    with pytest.raises(ValueError, match="2-D greyscale"):
        detect_streaks(np.zeros((100, 100, 3), dtype=np.float32))


def test_results_are_ordered_by_detection_score() -> None:
    image = with_stars(blank())
    image = draw_line(image, 50.0, 80.0, 700.0, 120.0)
    image = draw_line(image, 120.0, 500.0, 500.0, 200.0)
    scores = [s.detection_score for s in detect_streaks(image)]
    assert scores == sorted(scores, reverse=True)
