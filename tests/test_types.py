"""Tests for the public data model.

These mostly pin down the invariants that stop a later milestone from returning
a confident-looking result it has not earned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from satstreak.types import Candidate, IdentifyResult, Observation, Status

SOFIA = {"latitude_deg": 42.6977, "longitude_deg": 23.3219, "elevation_m": 550.0}
WHEN = datetime(2026, 9, 21, 20, 30, tzinfo=timezone.utc)


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


def test_candidate_rejects_a_score_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError, match=r"score must be in \[0, 1\]"):
        Candidate(norad_id=25544, name="ISS (ZARYA)", score=1.4)


def test_result_requires_candidates_to_be_ordered_best_first() -> None:
    weak = Candidate(norad_id=25544, name="ISS (ZARYA)", score=0.4)
    strong = Candidate(norad_id=48274, name="TIANHE", score=0.9)
    with pytest.raises(ValueError, match="best-first"):
        IdentifyResult(status=Status.AMBIGUOUS, candidates=(weak, strong))


def test_result_requires_a_match_to_have_a_clear_leader() -> None:
    # Two equally good fits are an ambiguous result, not a match. Without this the
    # CLI would print whichever happened to sort first as though it were certain.
    tie_a = Candidate(norad_id=25544, name="ISS (ZARYA)", score=0.8)
    tie_b = Candidate(norad_id=48274, name="TIANHE", score=0.8)
    with pytest.raises(ValueError, match="clear leader"):
        IdentifyResult(status=Status.MATCH, candidates=(tie_a, tie_b))

    ambiguous = IdentifyResult(status=Status.AMBIGUOUS, candidates=(tie_a, tie_b))
    assert ambiguous.best == tie_a


@pytest.mark.parametrize("status", [Status.MATCH, Status.AMBIGUOUS])
def test_result_requires_candidates_when_it_claims_to_have_found_something(
    status: Status,
) -> None:
    with pytest.raises(ValueError, match="requires at least one candidate"):
        IdentifyResult(status=status)


@pytest.mark.parametrize("status", [Status.NO_MATCH, Status.NO_STREAK, Status.NO_POINTING])
def test_negative_results_need_no_candidates(status: Status) -> None:
    result = IdentifyResult(status=status, message="nothing found")
    assert result.best is None
    assert result.to_dict()["candidates"] == []


def test_the_three_negative_outcomes_stay_distinct() -> None:
    # Collapsing these would hide "could not tell where the camera pointed" behind
    # "no satellite matched", which is the failure most likely to happen in practice.
    assert len({Status.NO_MATCH, Status.NO_STREAK, Status.NO_POINTING}) == 3
