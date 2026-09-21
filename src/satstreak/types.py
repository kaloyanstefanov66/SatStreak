"""Public data model for SatStreak.

These types are the contract between the stages of the pipeline (EXIF reading,
plate solving, streak detection, orbit propagation, matching) and the contract
between the library and its callers. They are defined before the stages that
produce them so that each stage can be built and tested against a fixed shape.

The shape follows from what the tool is for. SatStreak is not told where to look:
it sweeps a photograph and reports what it finds, which may be nothing, or may be
several trails the photographer never noticed. So one image yields zero or more
independent findings, each with its own evidence and its own confidence, rather
than a single answer about a streak the user had already spotted.

That framing also sets the error budget. When a caller has pointed at a streak,
a wrong name is a wrong answer. When the tool volunteers a finding, the caller
has no way to tell a real trail from an aircraft, a meteor, a cosmic ray hit, a
hot pixel or a power line — so a confident false positive is far more damaging
than a miss. Every invariant below that looks strict is there to make silence
cheaper than a guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ImageStatus(str, Enum):
    """What happened to the photograph as a whole."""

    FOUND = "found"
    """The image was solved and at least one streak was detected. Each finding
    carries its own outcome; some may still be unidentified."""

    NO_STREAKS = "no_streaks"
    """The image was solved and swept, and nothing streak-like was found."""

    NO_POINTING = "no_pointing"
    """The image could not be plate-solved, so the camera's aim is unknown and
    nothing downstream can be trusted. Kept distinct from NO_STREAKS because this
    is the failure most likely to occur in practice, and reporting it as 'nothing
    found' would hide it behind a far rarer result."""


class FindingStatus(str, Enum):
    """What happened to one detected streak."""

    MATCH = "match"
    """One candidate scored above threshold and its rivals did not."""

    AMBIGUOUS = "ambiguous"
    """Several satellites fit the track well enough to be indistinguishable."""

    UNIDENTIFIED = "unidentified"
    """A streak is present but no catalogued satellite fits it. An aircraft, a
    meteor, a sensor artefact and an uncatalogued object all land here; the tool
    does not claim to tell them apart."""


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
class Streak:
    """A trail found in the image, described in pixels and nothing more.

    Deliberately free of interpretation. What the streak *is* belongs to the
    Finding that wraps it; keeping the measurement separate means the detector
    can be evaluated on its own, which is necessary because detection precision
    is a different number from match accuracy and has to be reported separately.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    detection_score: float
    """Confidence in [0, 1] that this is a real trail and not sensor noise."""

    signal_to_noise: float | None = None
    """Peak brightness above the local background, in units of its noise."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.detection_score <= 1.0 or math.isnan(self.detection_score):
            raise ValueError(f"detection_score must be in [0, 1], got {self.detection_score}")
        if self.length_px <= 0:
            raise ValueError("a streak needs two distinct endpoints; got zero length")

    @property
    def length_px(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    @property
    def angle_deg(self) -> float:
        """Orientation in [0, 180). A trail has an axis, not a direction: which
        end the object started from is not knowable from the pixels alone."""
        return math.degrees(math.atan2(self.y2 - self.y1, self.x2 - self.x1)) % 180.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "length_px": round(self.length_px, 2),
            "angle_deg": round(self.angle_deg, 2),
            "detection_score": round(self.detection_score, 4),
            "signal_to_noise": self.signal_to_noise,
        }


@dataclass(frozen=True)
class Candidate:
    """A satellite that might have made a streak, with the evidence for it."""

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
class Finding:
    """One detected streak together with what, if anything, it was matched to."""

    streak: Streak
    status: FindingStatus
    candidates: tuple[Candidate, ...] = ()
    """Ordered best-first. Empty for UNIDENTIFIED."""

    message: str = ""

    def __post_init__(self) -> None:
        scores = [c.score for c in self.candidates]
        if scores != sorted(scores, reverse=True):
            raise ValueError("candidates must be ordered best-first by score")
        if self.status in (FindingStatus.MATCH, FindingStatus.AMBIGUOUS) and not self.candidates:
            raise ValueError(f"status {self.status.value} requires at least one candidate")
        if self.status is FindingStatus.UNIDENTIFIED and self.candidates:
            raise ValueError("an unidentified streak must not carry candidates")
        # Two equally good fits are ambiguous, not a match. Without this the tool
        # would present whichever sorted first as though it were certain, and a
        # caller who did not know the streak was there could not tell.
        if (
            self.status is FindingStatus.MATCH
            and len(self.candidates) > 1
            and self.candidates[0].score <= self.candidates[1].score
        ):
            raise ValueError("status match requires a single clear leader")

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "streak": self.streak.to_dict(),
            "status": self.status.value,
            "candidates": [c.to_dict() for c in self.candidates],
            "message": self.message,
        }


@dataclass(frozen=True)
class IdentifyResult:
    """The answer for one photograph: everything found in it, or why nothing was."""

    status: ImageStatus
    findings: tuple[Finding, ...] = ()
    message: str = ""
    """Plain-language explanation, shown to the user on any non-FOUND outcome."""

    diagnostics: dict[str, Any] = field(default_factory=dict)
    """Intermediate values (solved pointing, search window, detector settings)
    kept for the evaluation harness and for debugging bad calls."""

    def __post_init__(self) -> None:
        if self.status is ImageStatus.FOUND and not self.findings:
            raise ValueError("status found requires at least one finding")
        if self.status is not ImageStatus.FOUND and self.findings:
            raise ValueError(f"status {self.status.value} must not carry findings")

    @property
    def matched(self) -> tuple[Finding, ...]:
        """Findings confidently tied to a named satellite."""
        return tuple(f for f in self.findings if f.status is FindingStatus.MATCH)

    @property
    def unidentified(self) -> tuple[Finding, ...]:
        """Streaks that are real enough to report but match nothing catalogued."""
        return tuple(f for f in self.findings if f.status is FindingStatus.UNIDENTIFIED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "findings": [f.to_dict() for f in self.findings],
            "message": self.message,
            "diagnostics": self.diagnostics,
        }
