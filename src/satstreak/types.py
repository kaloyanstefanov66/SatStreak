"""Public data model for satstreak.

These types are the contract between the stages of the pipeline (EXIF reading,
plate solving, streak detection, orbit propagation, matching) and the contract
between the library and its callers. They are defined before the stages that
produce them so that each stage can be built and tested against a fixed shape.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Status(str, Enum):
    """Outcome of an identification attempt.

    ``NO_POINTING`` and ``NO_STREAK`` are deliberately distinct from ``NO_MATCH``.
    Failing to work out where the camera was aimed is a different failure from
    working it out and finding nothing there, and conflating them would hide the
    most common real-world failure behind a confident-sounding negative.
    """

    MATCH = "match"
    """One candidate scored above the confidence threshold and its rivals did not."""

    AMBIGUOUS = "ambiguous"
    """Several candidates fit the track and timing well enough to be indistinguishable."""

    NO_MATCH = "no_match"
    """Pointing and streak were both recovered, but no satellite fits the track."""

    NO_STREAK = "no_streak"
    """Pointing was recovered, but no streak was detected in the image."""

    NO_POINTING = "no_pointing"
    """The image could not be plate-solved, so the camera's aim is unknown."""


@dataclass(frozen=True)
class Observation:
    """Everything known about when and where a photograph was taken.

    Values normally come from EXIF but any of them may be supplied by the user,
    since phone cameras routinely omit GPS and some strip timestamps.
    """

    timestamp: datetime
    """Shutter-open time. Must be timezone-aware; naive values are rejected."""

    latitude_deg: float
    longitude_deg: float
    elevation_m: float = 0.0

    exposure_s: float | None = None
    """Shutter duration. Sets how far along its track a satellite travels."""

    timestamp_uncertainty_s: float = 2.0
    """How far the timestamp may be wrong.

    Phone clocks drift and EXIF times are recorded with second granularity, so a
    couple of seconds is the realistic floor. This widens the search window
    rather than shifting it; a timing error moves a satellite along its ground
    track, not off it, which is why geometry carries more weight than timing in
    the matcher.
    """

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError(
                "Observation.timestamp must be timezone-aware; a naive datetime is "
                "ambiguous and would silently shift the sky by hours."
            )
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise ValueError(f"latitude_deg out of range: {self.latitude_deg}")
        if not -180.0 <= self.longitude_deg <= 180.0:
            raise ValueError(f"longitude_deg out of range: {self.longitude_deg}")
        if self.exposure_s is not None and self.exposure_s <= 0:
            raise ValueError(f"exposure_s must be positive, got {self.exposure_s}")
        if self.timestamp_uncertainty_s < 0:
            raise ValueError("timestamp_uncertainty_s must not be negative")

    @property
    def utc(self) -> datetime:
        return self.timestamp.astimezone(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.utc.isoformat(),
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "elevation_m": self.elevation_m,
            "exposure_s": self.exposure_s,
            "timestamp_uncertainty_s": self.timestamp_uncertainty_s,
        }


@dataclass(frozen=True)
class Candidate:
    """A satellite that might have made the streak, with the evidence for it."""

    norad_id: int
    name: str
    score: float
    """Confidence in [0, 1]. Calibrated against the evaluation set, not a raw fit
    residual, so that a score of 0.9 means roughly nine in ten such calls are right."""

    angular_separation_deg: float | None = None
    """Distance between the predicted track and the observed streak."""

    direction_error_deg: float | None = None
    """Angle between the predicted and observed directions of travel."""

    length_ratio: float | None = None
    """Observed streak length divided by the length predicted from the exposure."""

    notes: tuple[str, ...] = ()
    """Human-readable remarks, e.g. why a close geometric fit was down-weighted."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0 or math.isnan(self.score):
            raise ValueError(f"score must be in [0, 1], got {self.score}")
        if self.norad_id <= 0:
            raise ValueError(f"norad_id must be positive, got {self.norad_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "norad_id": self.norad_id,
            "name": self.name,
            "score": round(self.score, 4),
            "angular_separation_deg": self.angular_separation_deg,
            "direction_error_deg": self.direction_error_deg,
            "length_ratio": self.length_ratio,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class IdentifyResult:
    """The answer returned to the caller, successful or not."""

    status: Status
    candidates: tuple[Candidate, ...] = ()
    """Ordered best-first. Non-empty only for MATCH and AMBIGUOUS."""

    message: str = ""
    """Plain-language explanation, shown to the user on any non-MATCH outcome."""

    diagnostics: dict[str, Any] = field(default_factory=dict)
    """Intermediate values (solved pointing, detected streak, search window) kept
    for the evaluation harness and for debugging bad calls."""

    def __post_init__(self) -> None:
        scores = [c.score for c in self.candidates]
        if scores != sorted(scores, reverse=True):
            raise ValueError("candidates must be ordered best-first by score")
        if self.status in (Status.MATCH, Status.AMBIGUOUS) and not self.candidates:
            raise ValueError(f"status {self.status.value} requires at least one candidate")
        if self.status is Status.MATCH and len(self.candidates) > 1:
            best, runner_up = self.candidates[0].score, self.candidates[1].score
            if best <= runner_up:
                raise ValueError("status match requires a single clear leader")

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "candidates": [c.to_dict() for c in self.candidates],
            "message": self.message,
            "diagnostics": self.diagnostics,
        }
