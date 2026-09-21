"""Tests for the public data model.

These mostly pin down the invariants that stop a later milestone from reporting a
finding it has not earned. That matters more here than in a tool the user aims at
a streak they already saw: when SatStreak volunteers a detection, the user has no
independent way to check it, so every path that could produce a confident false
positive is closed deliberately.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from satstreak.types import (
    Candidate,
    Finding,
    FindingStatus,
    IdentifyResult,
    ImageStatus,
    Observation,
    Streak,
)

SOFIA = {"latitude_deg": 42.6977, "longitude_deg": 23.3219, "elevation_m": 550.0}
WHEN = datetime(2026, 9, 21, 20, 30, tzinfo=timezone.utc)


def a_streak(score: float = 0.9) -> Streak:
    return Streak(x1=100.0, y1=100.0, x2=400.0, y2=400.0, detection_score=score)


def a_candidate(norad_id: int = 25544, name: str = "ISS (ZARYA)", score: float = 0.9) -> Candidate:
    return Candidate(norad_id=norad_id, name=name, score=score)


# --- Observation ------------------------------------------------------------


def test_observation_accepts_a_timezone_aware_timestamp() -> None:
    obs = Observation(timestamp=WHEN, exposure_s=8.0, **SOFIA)
    assert obs.utc == WHEN
    assert obs.to_dict()["timestamp"] == "2026-09-21T20:30:00+00:00"


def test_observation_normalises_a_non_utc_timestamp() -> None:
    eest = timezone(timedelta(hours=3))
    obs = Observation(timestamp=WHEN.astimezone(eest), **SOFIA)
    assert obs.utc == WHEN


def test_observation_rejects_a_naive_timestamp() -> None:
    # A naive timestamp read as local time when it was UTC rotates the sky by the
    # observer's offset, which would quietly produce a wrong but plausible match.
    with pytest.raises(ValueError, match="timezone-aware"):
        Observation(timestamp=datetime(2026, 9, 21, 20, 30), **SOFIA)


@pytest.mark.parametrize(
    ("field", "value"),
    [("latitude_deg", 91.0), ("longitude_deg", -181.0), ("exposure_s", 0.0)],
)
def test_observation_rejects_out_of_range_values(field: str, value: float) -> None:
    kwargs = {"timestamp": WHEN, **SOFIA, field: value}
    with pytest.raises(ValueError):
        Observation(**kwargs)


# --- Streak -----------------------------------------------------------------


def test_streak_measures_its_own_geometry() -> None:
    streak = Streak(x1=0.0, y1=0.0, x2=3.0, y2=4.0, detection_score=0.8)
    assert streak.length_px == pytest.approx(5.0)
    assert streak.angle_deg == pytest.approx(53.130, abs=1e-3)


def test_streak_angle_is_an_axis_not_a_direction() -> None:
    # Which end the object started from cannot be known from the pixels, so the
    # orientation wraps at 180 degrees and a reversed streak reads identically.
    forward = Streak(x1=0.0, y1=0.0, x2=10.0, y2=10.0, detection_score=0.8)
    reversed_ = Streak(x1=10.0, y1=10.0, x2=0.0, y2=0.0, detection_score=0.8)
    assert forward.angle_deg == pytest.approx(reversed_.angle_deg)
    assert 0.0 <= forward.angle_deg < 180.0


def test_streak_rejects_zero_length() -> None:
    with pytest.raises(ValueError, match="zero length"):
        Streak(x1=5.0, y1=5.0, x2=5.0, y2=5.0, detection_score=0.9)


def test_streak_rejects_a_detection_score_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError, match=r"detection_score must be in \[0, 1\]"):
        Streak(x1=0.0, y1=0.0, x2=1.0, y2=1.0, detection_score=1.2)


# --- Candidate and Finding --------------------------------------------------


def test_candidate_rejects_a_score_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError, match=r"score must be in \[0, 1\]"):
        a_candidate(score=1.4)


def test_finding_requires_candidates_to_be_ordered_best_first() -> None:
    weak = a_candidate(score=0.4)
    strong = a_candidate(norad_id=48274, name="TIANHE", score=0.9)
    with pytest.raises(ValueError, match="best-first"):
        Finding(streak=a_streak(), status=FindingStatus.AMBIGUOUS, candidates=(weak, strong))


def test_finding_requires_a_match_to_have_a_clear_leader() -> None:
    # Two equally good fits are ambiguous, not a match. Without this the tool
    # would present whichever sorted first as certain, and a user who did not
    # know the streak was there could not tell.
    tie_a = a_candidate(score=0.8)
    tie_b = a_candidate(norad_id=48274, name="TIANHE", score=0.8)
    with pytest.raises(ValueError, match="clear leader"):
        Finding(streak=a_streak(), status=FindingStatus.MATCH, candidates=(tie_a, tie_b))

    ambiguous = Finding(
        streak=a_streak(), status=FindingStatus.AMBIGUOUS, candidates=(tie_a, tie_b)
    )
    assert ambiguous.best == tie_a


@pytest.mark.parametrize("status", [FindingStatus.MATCH, FindingStatus.AMBIGUOUS])
def test_finding_requires_candidates_when_it_claims_to_have_found_something(
    status: FindingStatus,
) -> None:
    with pytest.raises(ValueError, match="requires at least one candidate"):
        Finding(streak=a_streak(), status=status)


def test_an_unidentified_finding_must_not_carry_candidates() -> None:
    # Reporting "unidentified" while still listing a satellite would let a caller
    # read the name as the answer. Unidentified means the evidence ran out.
    with pytest.raises(ValueError, match="must not carry candidates"):
        Finding(
            streak=a_streak(),
            status=FindingStatus.UNIDENTIFIED,
            candidates=(a_candidate(score=0.2),),
        )


# --- IdentifyResult ---------------------------------------------------------


def test_result_collects_several_findings_from_one_image() -> None:
    # The whole point of sweeping an image the user has not inspected: one
    # photograph can hold more than one trail.
    matched = Finding(streak=a_streak(), status=FindingStatus.MATCH, candidates=(a_candidate(),))
    unknown = Finding(
        streak=Streak(x1=0.0, y1=500.0, x2=200.0, y2=520.0, detection_score=0.7),
        status=FindingStatus.UNIDENTIFIED,
    )
    result = IdentifyResult(status=ImageStatus.FOUND, findings=(matched, unknown))

    assert len(result.findings) == 2
    assert result.matched == (matched,)
    assert result.unidentified == (unknown,)
    assert len(result.to_dict()["findings"]) == 2


def test_found_requires_at_least_one_finding() -> None:
    with pytest.raises(ValueError, match="requires at least one finding"):
        IdentifyResult(status=ImageStatus.FOUND)


@pytest.mark.parametrize("status", [ImageStatus.NO_STREAKS, ImageStatus.NO_POINTING])
def test_negative_image_results_must_not_carry_findings(status: ImageStatus) -> None:
    with pytest.raises(ValueError, match="must not carry findings"):
        IdentifyResult(
            status=status,
            findings=(
                Finding(streak=a_streak(), status=FindingStatus.MATCH, candidates=(a_candidate(),)),
            ),
        )


@pytest.mark.parametrize("status", [ImageStatus.NO_STREAKS, ImageStatus.NO_POINTING])
def test_negative_image_results_are_well_formed(status: ImageStatus) -> None:
    result = IdentifyResult(status=status, message="nothing found")
    assert result.matched == ()
    assert result.to_dict()["findings"] == []


def test_an_unsolved_image_is_distinct_from_an_empty_one() -> None:
    # Collapsing these would hide "could not tell where the camera pointed" behind
    # "no satellites in your photo", which is the failure most likely in practice
    # and the one a user would most readily believe.
    assert ImageStatus.NO_POINTING is not ImageStatus.NO_STREAKS
    assert len({ImageStatus.FOUND, ImageStatus.NO_STREAKS, ImageStatus.NO_POINTING}) == 3
