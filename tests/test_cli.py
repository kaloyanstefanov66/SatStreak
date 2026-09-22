"""Tests for the command-line layer.

The CLI is deliberately thin, so most of these check that it refuses clearly
rather than proceeding on a guess. The exception is the last test, which runs a
real photograph through the whole pipeline from the command line.
"""

from __future__ import annotations

from pathlib import Path

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


def _bare_photo(tmp_path: Path) -> Path:
    """A photograph with no metadata at all, as a stripped share produces."""
    pytest.importorskip("PIL", reason="needs Pillow")
    from PIL import Image

    path = tmp_path / "bare.jpg"
    Image.new("RGB", (400, 300)).save(path)
    return path


def _full_photo(tmp_path: Path) -> Path:
    """A photograph carrying time, position, exposure and focal length."""
    pytest.importorskip("PIL", reason="needs Pillow")
    from PIL import Image

    path = tmp_path / "full.jpg"
    img = Image.new("RGB", (400, 300))
    exif = img.getexif()
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = "2026:09:21 23:14:07"
    ifd[0x9011] = "+03:00"
    ifd[0x829A] = 20.0
    ifd[0xA405] = 26.0
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (42.0, 41.0, 51.7)
    gps[3], gps[4] = "E", (23.0, 19.0, 18.8)
    img.save(path, exif=exif)
    return path


def test_scan_of_a_missing_file_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", "no-such-photo.jpg"]) == EXIT_ERROR
    assert "Could not read" in capsys.readouterr().err


def test_scan_of_a_stripped_photo_names_what_it_needs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No metadata means the user has to supply it, and the message should say
    # which flags rather than failing generically.
    assert main(["scan", str(_bare_photo(tmp_path))]) == EXIT_ERROR
    error = capsys.readouterr().err
    assert "--time" in error
    assert "--lat and --lon" in error


def test_scan_rejects_a_time_without_an_offset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A local time read as UTC rotates the sky by the observer's offset, so this
    # refuses rather than assuming.
    code = main(["scan", str(_bare_photo(tmp_path)), "--time", "2026-09-21T23:14:07"])
    assert code == EXIT_ERROR
    assert "needs a UTC offset" in capsys.readouterr().err


def test_a_photo_with_metadata_only_needs_to_be_told_where_it_pointed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Time, position, exposure and field of view all come from the photograph.
    # The aim is the only thing left, because plate solving does not exist yet.
    assert main(["scan", str(_full_photo(tmp_path))]) == EXIT_ERROR
    error = capsys.readouterr().err
    assert "--alt and --az" in error
    assert "--time" not in error
    assert "--fov" not in error


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
