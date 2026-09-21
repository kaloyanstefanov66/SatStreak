"""Tests for the command-line layer."""

from __future__ import annotations

import json

import pytest

from satstreak.cli import EXIT_NO_MATCH, _render, main
from satstreak.types import (
    Candidate,
    Finding,
    FindingStatus,
    IdentifyResult,
    ImageStatus,
    Streak,
)


def test_scan_reports_that_pointing_is_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    # Until plate solving exists the only honest answer is that pointing is
    # unknown. This test is expected to change when milestone 6 lands, not to be
    # deleted.
    assert main(["scan", "photo.jpg", "--json"]) == EXIT_NO_MATCH
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "no_pointing"
    assert payload["findings"] == []
    assert payload["diagnostics"]["image"] == "photo.jpg"


def test_scan_without_json_prints_the_message(capsys: pytest.CaptureFixture[str]) -> None:
    main(["scan", "photo.jpg"])
    assert "Not implemented yet" in capsys.readouterr().out


def test_a_missing_subcommand_is_an_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0


def test_rendering_lists_every_finding_including_the_unidentified_one() -> None:
    # A user who did not know anything was in the photograph needs to see the
    # streaks that were found but not named, not just the confident ones.
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
