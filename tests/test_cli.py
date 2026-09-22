"""Tests for the command-line layer.

The CLI is deliberately thin, so most of these check that it refuses clearly
rather than proceeding on a guess. The exception is the last test, which runs a
real photograph through the whole pipeline from the command line.
"""

from __future__ import annotations

import pytest

from satstreak.cli import EXIT_ERROR, _render, main
from satstreak.types import (
    Candidate,
    Finding,
    FindingStatus,
    IdentifyResult,
    ImageStatus,
    Streak,
)


def test_a_missing_subcommand_is_an_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0


def test_scan_without_a_time_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", "photo.jpg"]) == EXIT_ERROR
    assert "--time is required" in capsys.readouterr().err


def test_scan_rejects_a_time_without_an_offset(capsys: pytest.CaptureFixture[str]) -> None:
    # A local time read as UTC rotates the sky by the observer's offset, so this
    # refuses rather than assuming.
    code = main(
        ["scan", "photo.jpg", "--time", "2026-09-21T23:14:07", "--lat", "42", "--lon", "23"]
    )
    assert code == EXIT_ERROR
    assert "needs a UTC offset" in capsys.readouterr().err


def test_scan_without_a_pointing_explains_what_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Plate solving would supply the aim. Until it does, the CLI says so instead
    # of guessing a pointing and reporting confident nonsense.
    code = main(
        ["scan", "photo.jpg", "--time", "2026-09-21T23:14:07+03:00", "--lat", "42", "--lon", "23"]
    )
    assert code == EXIT_ERROR
    error = capsys.readouterr().err
    assert "--alt, --az and --fov are required" in error
    assert "cannot yet recover the camera's aim" in error


# --- rendering --------------------------------------------------------------


def test_rendering_lists_every_finding_including_the_unidentified_one() -> None:
    # A user who did not know anything was in the photograph needs to see the
    # trails that were found but not named, not just the confident ones.
    result = IdentifyResult(
        status=ImageStatus.FOUND,
        findings=(
            Finding(
                streak=Streak(x1=0.0, y1=0.0, x2=300.0, y2=0.0, detection_score=0.95),
                status=FindingStatus.MATCH,
                candidates=(Candidate(norad_id=25544, name="ISS (ZARYA)", score=0.94),),
            ),
            Finding(
                streak=Streak(x1=0.0, y1=400.0, x2=120.0, y2=400.0, detection_score=0.6),
                status=FindingStatus.UNIDENTIFIED,
            ),
        ),
    )
    text = _render(result, as_json=False)
    assert "Found 2 trail(s):" in text
    assert "ISS (ZARYA) (NORAD 25544)" in text
    assert "94%" in text
    assert "unidentified" in text


def test_rendering_an_empty_sky_does_not_claim_a_failure() -> None:
    result = IdentifyResult(
        status=ImageStatus.NO_STREAKS,
        message="No satellite trails found in this photograph.",
    )
    assert _render(result, as_json=False) == "No satellite trails found in this photograph."
