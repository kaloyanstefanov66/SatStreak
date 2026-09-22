"""Reading what the camera recorded, so the user does not have to type it.

A photograph usually already knows when and where it was taken and how long the
shutter was open. Making somebody transcribe that from their phone's details
panel is a poor way to run a tool whose whole promise is "give it a photo".

The timestamp is the part that needs care, and it is why this module is not a
thin wrapper. ``DateTimeOriginal`` is **local time with no offset attached**, so
reading it as UTC rotates the sky by the observer's offset and produces a
confident wrong answer. Three sources are tried, best first:

1. ``GPSDateStamp`` and ``GPSTimeStamp``, which are already UTC and come from the
   satellites rather than from the phone's clock. Most phones that record
   position record these too.
2. ``DateTimeOriginal`` together with ``OffsetTimeOriginal``, which states the
   offset explicitly.
3. ``DateTimeOriginal`` alone — **refused**, because there is no honest way to
   turn it into an instant. The caller is told to pass the time by hand.

Everything else degrades gracefully: a missing field is reported as missing so
the caller can supply it, rather than being filled in with a plausible default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from satstreak.types import Observation

#: 35 mm full-frame sensor width, used to turn the EXIF 35 mm-equivalent focal
#: length into a field-of-view estimate.
FULL_FRAME_WIDTH_MM = 36.0

# EXIF tag numbers, named so the parsing below reads as something other than
# magic constants.
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_DATETIME = 0x0132
_IFD_EXIF = 0x8769
_IFD_GPS = 0x8825
_TAG_EXPOSURE_TIME = 0x829A
_TAG_ISO = 0x8827
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_OFFSET_TIME_ORIGINAL = 0x9011
_TAG_FOCAL_LENGTH_35MM = 0xA405
_GPS_LATITUDE_REF = 1
_GPS_LATITUDE = 2
_GPS_LONGITUDE_REF = 3
_GPS_LONGITUDE = 4
_GPS_ALTITUDE_REF = 5
_GPS_ALTITUDE = 6
_GPS_TIMESTAMP = 7
_GPS_DATESTAMP = 29


class ExifError(RuntimeError):
    """The photograph could not be read at all."""


@dataclass(frozen=True)
class ExifFacts:
    """What the camera recorded, and what it did not."""

    width: int | None = None
    height: int | None = None
    camera: str | None = None

    timestamp_utc: datetime | None = None
    timestamp_source: str = "none"
    """Where the instant came from: ``gps``, ``offset``, or ``none``. Worth
    surfacing because a GPS timestamp is authoritative while a phone clock plus a
    recorded offset can still drift by seconds."""

    latitude_deg: float | None = None
    longitude_deg: float | None = None
    elevation_m: float | None = None

    exposure_s: float | None = None
    iso: int | None = None
    focal_length_35mm: float | None = None
    fov_width_deg: float | None = None

    warnings: tuple[str, ...] = ()
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_complete(self) -> bool:
        """True when a scan could run from this photograph alone."""
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera": self.camera,
            "width": self.width,
            "height": self.height,
            "timestamp_utc": self.timestamp_utc.isoformat() if self.timestamp_utc else None,
            "timestamp_source": self.timestamp_source,
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "elevation_m": self.elevation_m,
            "exposure_s": self.exposure_s,
            "iso": self.iso,
            "focal_length_35mm": self.focal_length_35mm,
            "fov_width_deg": self.fov_width_deg,
            "missing": list(self.missing),
            "warnings": list(self.warnings),
        }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dms_to_degrees(dms: Any, ref: Any) -> float | None:
    try:
        degrees, minutes, seconds = (float(part) for part in dms)
    except (TypeError, ValueError):
        return None
    result = degrees + minutes / 60.0 + seconds / 3600.0
    if str(ref).strip().upper().startswith(("S", "W")):
        result = -result
    return result


def _utc_from_gps(gps: dict[int, Any]) -> datetime | None:
    """Build an instant from the GPS date and time stamps, which are UTC.

    Preferred over the camera's own clock: these come from the satellites, so
    they are neither ambiguous about offset nor subject to the phone's drift.
    """
    date_text = gps.get(_GPS_DATESTAMP)
    time_parts = gps.get(_GPS_TIMESTAMP)
    if not date_text or not time_parts:
        return None
    try:
        year, month, day = (int(part) for part in str(date_text).strip().split(":"))
        hours, minutes, seconds = (float(part) for part in time_parts)
    except (TypeError, ValueError):
        return None
    try:
        return datetime(year, month, day, tzinfo=timezone.utc) + timedelta(
            hours=hours, minutes=minutes, seconds=seconds
        )
    except ValueError:
        return None


def _utc_from_clock(ifd: dict[int, Any], base: dict[int, Any]) -> tuple[datetime | None, str]:
    """Build an instant from the camera clock, only if the offset is recorded."""
    raw = ifd.get(_TAG_DATETIME_ORIGINAL) or base.get(_TAG_DATETIME)
    if not raw:
        return None, "none"
    try:
        naive = datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None, "none"

    offset_text = ifd.get(_TAG_OFFSET_TIME_ORIGINAL)
    if not offset_text:
        # Deliberately not assumed to be UTC. A local time read as UTC rotates
        # the sky by the observer's offset, which yields a wrong but entirely
        # plausible identification.
        return None, "none"
    try:
        text = str(offset_text).strip()
        sign = -1 if text[0] == "-" else 1
        hours, minutes = (int(part) for part in text[1:].split(":"))
        offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
    except (ValueError, IndexError):
        return None, "none"
    return naive.replace(tzinfo=offset).astimezone(timezone.utc), "offset"


def read_exif(path: str | Path) -> ExifFacts:
    """Read the fields SatStreak needs from a photograph.

    Never raises for missing metadata; a photograph with none at all returns
    facts full of ``None`` and a populated ``missing`` list. Only an unreadable
    file raises.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - optional extra
        raise ExifError(
            "Reading photographs needs Pillow. Install it with: pip install satstreak[images]"
        ) from exc

    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(path) as img:
            width, height = img.size
            exif = img.getexif()
            base = dict(exif)
            ifd = dict(exif.get_ifd(_IFD_EXIF))
            gps = dict(exif.get_ifd(_IFD_GPS))
    except OSError as exc:
        raise ExifError(f"Could not read {path}: {exc}") from exc

    warnings: list[str] = []

    make, model = base.get(_TAG_MAKE), base.get(_TAG_MODEL)
    camera = " ".join(str(p).strip() for p in (make, model) if p).strip() or None

    timestamp = _utc_from_gps(gps)
    source = "gps" if timestamp else "none"
    if timestamp is None:
        timestamp, source = _utc_from_clock(ifd, base)
        if timestamp is None and (ifd.get(_TAG_DATETIME_ORIGINAL) or base.get(_TAG_DATETIME)):
            warnings.append(
                "the photograph records a time but no UTC offset, so the instant is "
                "ambiguous; pass --time with an offset"
            )

    latitude = _dms_to_degrees(gps.get(_GPS_LATITUDE), gps.get(_GPS_LATITUDE_REF))
    longitude = _dms_to_degrees(gps.get(_GPS_LONGITUDE), gps.get(_GPS_LONGITUDE_REF))
    altitude = _as_float(gps.get(_GPS_ALTITUDE))
    if altitude is not None and gps.get(_GPS_ALTITUDE_REF) in (1, b"\x01"):
        altitude = -altitude  # below sea level

    exposure = _as_float(ifd.get(_TAG_EXPOSURE_TIME))
    iso_value = ifd.get(_TAG_ISO)
    focal_35 = _as_float(ifd.get(_TAG_FOCAL_LENGTH_35MM))

    fov = None
    if focal_35:
        fov = round(2.0 * math.degrees(math.atan(FULL_FRAME_WIDTH_MM / (2.0 * focal_35))), 3)

    missing = tuple(
        name
        for name, value in (
            ("time", timestamp),
            ("location", latitude if latitude is not None else None),
            ("exposure", exposure),
            ("field of view", fov),
        )
        if value is None
    )

    if exposure is not None and exposure < 1.0:
        warnings.append(
            f"the exposure was {exposure:g}s, which is short for a satellite trail; "
            "any trail will be correspondingly short"
        )

    return ExifFacts(
        width=width,
        height=height,
        camera=camera,
        timestamp_utc=timestamp,
        timestamp_source=source,
        latitude_deg=latitude,
        longitude_deg=longitude,
        elevation_m=altitude,
        exposure_s=exposure,
        iso=int(iso_value) if isinstance(iso_value, int) else None,
        focal_length_35mm=focal_35,
        fov_width_deg=fov,
        warnings=tuple(warnings),
        missing=missing,
    )


def observation_from(
    facts: ExifFacts,
    *,
    timestamp: datetime | None = None,
    latitude_deg: float | None = None,
    longitude_deg: float | None = None,
    elevation_m: float | None = None,
    exposure_s: float | None = None,
) -> Observation:
    """Combine EXIF with explicit values, which always win.

    Raises:
        ExifError: Naming exactly what is still missing, so the caller can tell
            the user which flags to pass rather than reporting a generic failure.
    """
    when = timestamp or facts.timestamp_utc
    latitude = latitude_deg if latitude_deg is not None else facts.latitude_deg
    longitude = longitude_deg if longitude_deg is not None else facts.longitude_deg

    absent: list[str] = []
    if when is None:
        absent.append("--time (no usable timestamp in the photograph)")
    if latitude is None or longitude is None:
        absent.append("--lat and --lon (no GPS position in the photograph)")
    if absent:
        raise ExifError("Missing: " + "; ".join(absent))

    return Observation(
        timestamp=when,
        latitude_deg=latitude,
        longitude_deg=longitude,
        elevation_m=(
            elevation_m
            if elevation_m is not None
            else (facts.elevation_m if facts.elevation_m is not None else 0.0)
        ),
        exposure_s=exposure_s if exposure_s is not None else facts.exposure_s,
    )
