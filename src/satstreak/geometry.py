"""Mapping the sky onto the sensor, and back.

This is the join between the two halves of the problem. Propagation says where a
satellite was in the observer's sky, in altitude and azimuth. Detection says
where a trail is, in pixels. Neither can be compared with the other until both
live in the same frame, and that is what this module provides.

Everything works in the horizontal (alt/az) frame rather than in equatorial
coordinates. Satellites are naturally topocentric, exposures are seconds long so
the sky does not turn appreciably during one, and a camera on a tripod is aimed
at a patch of *sky above the observer* rather than at a patch of celestial
sphere. A plate solve returns right ascension and declination, which is converted
into this frame once, at the shutter time, instead of converting every satellite
the other way.

The projection is gnomonic (tangent plane), the same model a plate solve
produces. It is exact for an ideal rectilinear lens and degrades as the field
widens, which is a limitation worth stating plainly rather than hiding: across a
70 degree phone frame, real lens distortion departs from this model near the
corners. `Pointing.is_wide_field` flags the regime where that matters.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

from satstreak.propagate import LookAngles, Track

#: Beyond this the tangent-plane model and real lenses part company enough that
#: corner positions should not be trusted without a distortion term.
WIDE_FIELD_THRESHOLD_DEG = 60.0


@dataclass(frozen=True)
class PixelPoint:
    x: float
    y: float


@dataclass(frozen=True)
class PixelTrack:
    """A satellite's path across the exposure, in image coordinates."""

    norad_id: int
    name: str
    points: tuple[PixelPoint | None, ...]
    """One entry per track sample. None where the satellite was outside the frame
    or behind the camera, so that a path entering mid-exposure stays legible."""

    @property
    def visible_points(self) -> tuple[PixelPoint, ...]:
        return tuple(p for p in self.points if p is not None)

    @property
    def crosses_frame(self) -> bool:
        return len(self.visible_points) >= 2

    @property
    def length_px(self) -> float:
        """Length of the visible portion of the path."""
        seen = self.visible_points
        if len(seen) < 2:
            return 0.0
        return sum(math.hypot(b.x - a.x, b.y - a.y) for a, b in itertools.pairwise(seen))

    @property
    def angle_deg(self) -> float | None:
        """Orientation of the path in [0, 180), matching `Streak.angle_deg`.

        A trail in an image has an axis rather than a direction, so this wraps at
        180 to be directly comparable with a detected streak, which cannot know
        which end the object started from.
        """
        seen = self.visible_points
        if len(seen) < 2:
            return None
        first, last = seen[0], seen[-1]
        return math.degrees(math.atan2(last.y - first.y, last.x - first.x)) % 180.0


@dataclass(frozen=True)
class Pointing:
    """Where the camera was aimed, and how its sky maps to its sensor.

    Normally recovered by plate solving. Can also be supplied by hand, which is
    what makes the rest of the pipeline testable and usable without a solver.
    """

    altitude_deg: float
    """Altitude of the frame centre, degrees above the horizon."""

    azimuth_deg: float
    """Azimuth of the frame centre, degrees clockwise from north."""

    roll_deg: float
    """Rotation of the sensor about the optical axis. Zero means the image's +y
    axis points towards the zenith."""

    scale_arcsec_per_px: float
    width_px: int
    height_px: int

    def __post_init__(self) -> None:
        if not -90.0 <= self.altitude_deg <= 90.0:
            raise ValueError(f"altitude_deg out of range: {self.altitude_deg}")
        if self.scale_arcsec_per_px <= 0:
            raise ValueError("scale_arcsec_per_px must be positive")
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("image dimensions must be positive")

    @property
    def field_width_deg(self) -> float:
        return self.width_px * self.scale_arcsec_per_px / 3600.0

    @property
    def field_height_deg(self) -> float:
        return self.height_px * self.scale_arcsec_per_px / 3600.0

    @property
    def is_wide_field(self) -> bool:
        """True where the tangent-plane model starts to misrepresent a real lens.

        Phone cameras sit here: a main camera spans about 70 degrees and an
        ultra-wide 100 or more. Positions near the centre stay good; corners drift
        without a distortion term.
        """
        return self.field_width_deg > WIDE_FIELD_THRESHOLD_DEG

    # -- projection ---------------------------------------------------------

    def _basis(self) -> tuple[tuple[float, float, float], ...]:
        """Right-handed basis with the optical axis last.

        Vectors are in a local horizontal frame: x east, y north, z up.
        """
        alt = math.radians(self.altitude_deg)
        az = math.radians(self.azimuth_deg)

        # Optical axis, pointing out of the lens.
        forward = (
            math.cos(alt) * math.sin(az),
            math.cos(alt) * math.cos(az),
            math.sin(alt),
        )
        # "Right" is horizontal and perpendicular to the aim, which stays
        # well-defined as altitude approaches the zenith where azimuth does not.
        right = (math.cos(az), -math.sin(az), 0.0)
        # Completes the frame. The order matters: right x forward points towards
        # the zenith, while forward x right points below the horizon and would
        # mirror every projected track vertically.
        up = (
            right[1] * forward[2] - right[2] * forward[1],
            right[2] * forward[0] - right[0] * forward[2],
            right[0] * forward[1] - right[1] * forward[0],
        )
        return right, up, forward

    def project(self, angles: LookAngles) -> PixelPoint | None:
        """Where a direction in the sky lands on the sensor.

        Returns None when the direction is behind the camera or outside the
        frame. Outside is a real answer, not an error: most of the catalogue is
        outside any given frame at any moment.
        """
        alt = math.radians(angles.altitude_deg)
        az = math.radians(angles.azimuth_deg)
        target = (
            math.cos(alt) * math.sin(az),
            math.cos(alt) * math.cos(az),
            math.sin(alt),
        )

        right, up, forward = self._basis()
        depth = sum(t * f for t, f in zip(target, forward, strict=True))
        if depth <= 1e-9:
            return None  # at or behind the focal plane

        # Gnomonic projection: divide the transverse components by the depth.
        tangent_x = sum(t * r for t, r in zip(target, right, strict=True)) / depth
        tangent_y = sum(t * u for t, u in zip(target, up, strict=True)) / depth

        pixels_per_radian = 3600.0 * math.degrees(1.0) / self.scale_arcsec_per_px
        offset_x = tangent_x * pixels_per_radian
        offset_y = tangent_y * pixels_per_radian

        roll = math.radians(self.roll_deg)
        rotated_x = offset_x * math.cos(roll) - offset_y * math.sin(roll)
        rotated_y = offset_x * math.sin(roll) + offset_y * math.cos(roll)

        # Image coordinates have y increasing downwards, so up on the sky is
        # subtracted rather than added.
        x = self.width_px / 2.0 + rotated_x
        y = self.height_px / 2.0 - rotated_y

        if not (0.0 <= x <= self.width_px and 0.0 <= y <= self.height_px):
            return None
        return PixelPoint(x=x, y=y)

    def contains(self, angles: LookAngles) -> bool:
        return self.project(angles) is not None

    def project_track(self, track: Track) -> PixelTrack:
        """Map a propagated track into image coordinates."""
        return PixelTrack(
            norad_id=track.norad_id,
            name=track.name,
            points=tuple(self.project(sample) for sample in track.samples),
        )
