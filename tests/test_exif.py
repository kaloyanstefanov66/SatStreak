"""Tests for reading a photograph's own metadata.

The timestamp cases carry most of the weight. A local time read as UTC rotates
the sky by the observer's offset, which produces a wrong answer that looks
entirely reasonable, so the rules about which clock is trusted are pinned
explicitly rather than left to behave sensibly by accident.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from satstreak.exif import ExifError, observation_from, read_exif

pytest.importorskip("PIL", reason="EXIF handling needs Pillow")
from PIL import Image

# Sofia, roughly: 42.6977 N, 23.3219 E.
SOFIA_LAT_DMS = (42.0, 41.0, 51.7)
SOFIA_LON_DMS = (23.0, 19.0, 18.8)


def write_photo(
    path: Path,
    *,
    datetime_original: str | None = "2026:09:21 23:14:07",
    offset: str | None = "+03:00",
    gps_date: str | None = None,
    gps_time: tuple[float, float, float] | None = None,
    gps_position: bool = True,
    exposure: float | None = 20.0,
    focal_35: float | None = 26.0,
    iso: int | None = 1600,
    size: tuple[int, int] = (400, 300),
) -> Path:
    """Write a small JPEG carrying exactly the tags a test cares about."""
    img = Image.new("RGB", size, (8, 10, 20))
    exif = img.getexif()
    exif[0x010F] = "SyntheticCo"
    exif[0x0110] = "TestPhone 1"

    ifd = exif.get_ifd(0x8769)
    if datetime_original:
        ifd[0x9003] = datetime_original
    if offset:
        ifd[0x9011] = offset
    if exposure is not None:
        ifd[0x829A] = exposure
    if iso is not None:
        ifd[0x8827] = iso
    if focal_35 is not None:
        ifd[0xA405] = focal_35

    gps = exif.get_ifd(0x8825)
    if gps_position:
        gps[1] = "N"
        gps[2] = SOFIA_LAT_DMS
        gps[3] = "E"
        gps[4] = SOFIA_LON_DMS
        gps[5] = 0
        gps[6] = 550.0
    if gps_date:
        gps[29] = gps_date
    if gps_time:
        gps[7] = gps_time

    img.save(path, exif=exif)
    return path


# --- the timestamp ----------------------------------------------------------


def test_gps_time_is_preferred_and_is_already_utc(tmp_path: Path) -> None:
    # GPS stamps come from the satellites: no offset ambiguity, no clock drift.
    # Here the camera clock deliberately disagrees, and GPS must win.
    path = write_photo(
        tmp_path / "gps.jpg",
        datetime_original="2026:09:21 23:14:07",
        offset="+03:00",
        gps_date="2026:09:21",
        gps_time=(20.0, 14.0, 9.0),
    )
    facts = read_exif(path)
    assert facts.timestamp_source == "gps"
    assert facts.timestamp_utc == datetime(2026, 9, 21, 20, 14, 9, tzinfo=timezone.utc)


def test_a_recorded_offset_is_used_when_there_is_no_gps_time(tmp_path: Path) -> None:
    path = write_photo(tmp_path / "offset.jpg")
    facts = read_exif(path)
    assert facts.timestamp_source == "offset"
    # 23:14:07 at +03:00 is 20:14:07 UTC.
    assert facts.timestamp_utc == datetime(2026, 9, 21, 20, 14, 7, tzinfo=timezone.utc)


def test_a_time_without_an_offset_is_refused_rather_than_assumed(tmp_path: Path) -> None:
    # The important one. Treating this as UTC would shift the sky by three hours
    # for a Sofia photograph and still return a confident identification.
    path = write_photo(tmp_path / "naive.jpg", offset=None)
    facts = read_exif(path)
    assert facts.timestamp_utc is None
    assert facts.timestamp_source == "none"
    assert any("no UTC offset" in w for w in facts.warnings)
    assert "time" in facts.missing


def test_a_negative_offset_is_handled(tmp_path: Path) -> None:
    path = write_photo(
        tmp_path / "west.jpg", datetime_original="2026:09:21 18:14:07", offset="-05:00"
    )
    facts = read_exif(path)
    assert facts.timestamp_utc == datetime(2026, 9, 21, 23, 14, 7, tzinfo=timezone.utc)


# --- position and optics ----------------------------------------------------


def test_position_is_decoded_from_the_gps_tags(tmp_path: Path) -> None:
    facts = read_exif(write_photo(tmp_path / "pos.jpg"))
    assert facts.latitude_deg == pytest.approx(42.6977, abs=1e-4)
    assert facts.longitude_deg == pytest.approx(23.3219, abs=1e-4)
    assert facts.elevation_m == pytest.approx(550.0)


def test_southern_and_western_hemispheres_are_negative(tmp_path: Path) -> None:
    # A dropped sign here puts the observer on the wrong side of the planet and
    # silently changes the whole sky.
    path = tmp_path / "south.jpg"
    img = Image.new("RGB", (100, 100))
    exif = img.getexif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "S", (33.0, 52.0, 0.0)
    gps[3], gps[4] = "W", (151.0, 12.0, 0.0)
    img.save(path, exif=exif)

    facts = read_exif(path)
    assert facts.latitude_deg == pytest.approx(-33.8667, abs=1e-3)
    assert facts.longitude_deg == pytest.approx(-151.2, abs=1e-3)


def test_field_of_view_is_derived_from_the_focal_length(tmp_path: Path) -> None:
    # A 26mm-equivalent phone lens spans about 69.4 degrees across the frame.
    facts = read_exif(write_photo(tmp_path / "fov.jpg", focal_35=26.0))
    assert facts.focal_length_35mm == 26.0
    assert facts.fov_width_deg == pytest.approx(69.39, abs=0.05)


def test_exposure_and_camera_are_read(tmp_path: Path) -> None:
    facts = read_exif(write_photo(tmp_path / "meta.jpg"))
    assert facts.exposure_s == pytest.approx(20.0)
    assert facts.iso == 1600
    assert facts.camera == "SyntheticCo TestPhone 1"
    assert (facts.width, facts.height) == (400, 300)


def test_a_short_exposure_is_flagged(tmp_path: Path) -> None:
    facts = read_exif(write_photo(tmp_path / "short.jpg", exposure=0.02))
    assert any("short for a satellite trail" in w for w in facts.warnings)


# --- absent metadata --------------------------------------------------------


def test_a_photograph_with_no_metadata_reports_what_is_missing(tmp_path: Path) -> None:
    # A stripped photograph is not an error; it is a photograph the caller must
    # supply details for.
    path = tmp_path / "bare.jpg"
    Image.new("RGB", (200, 150)).save(path)

    facts = read_exif(path)
    assert not facts.is_complete
    assert set(facts.missing) == {"time", "location", "exposure", "field of view"}
    assert facts.timestamp_utc is None


def test_a_complete_photograph_says_so(tmp_path: Path) -> None:
    facts = read_exif(write_photo(tmp_path / "full.jpg"))
    assert facts.is_complete
    assert facts.missing == ()


def test_an_unreadable_file_raises(tmp_path: Path) -> None:
    broken = tmp_path / "not-an-image.jpg"
    broken.write_text("this is not a JPEG", encoding="utf-8")
    with pytest.raises(ExifError, match="Could not read"):
        read_exif(broken)


# --- building an Observation ------------------------------------------------


def test_an_observation_is_built_from_the_photograph_alone(tmp_path: Path) -> None:
    observation = observation_from(read_exif(write_photo(tmp_path / "full.jpg")))
    assert observation.utc == datetime(2026, 9, 21, 20, 14, 7, tzinfo=timezone.utc)
    assert observation.latitude_deg == pytest.approx(42.6977, abs=1e-4)
    assert observation.exposure_s == pytest.approx(20.0)
    assert observation.elevation_m == pytest.approx(550.0)


def test_explicit_values_override_the_photograph(tmp_path: Path) -> None:
    # For cameras that record nothing, or record it wrongly.
    facts = read_exif(write_photo(tmp_path / "full.jpg"))
    chosen = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    observation = observation_from(
        facts, timestamp=chosen, latitude_deg=0.0, longitude_deg=0.0, exposure_s=5.0
    )
    assert observation.utc == chosen
    assert observation.latitude_deg == 0.0
    assert observation.exposure_s == 5.0


def test_building_an_observation_names_exactly_what_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "bare.jpg"
    Image.new("RGB", (200, 150)).save(path)
    facts = read_exif(path)

    with pytest.raises(ExifError) as exc:
        observation_from(facts)
    message = str(exc.value)
    assert "--time" in message
    assert "--lat and --lon" in message


def test_a_photograph_missing_only_position_asks_only_for_position(tmp_path: Path) -> None:
    facts = read_exif(write_photo(tmp_path / "notime.jpg", gps_position=False))
    with pytest.raises(ExifError) as exc:
        observation_from(facts)
    message = str(exc.value)
    assert "--lat and --lon" in message
    assert "--time" not in message
