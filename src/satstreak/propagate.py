"""Turning orbital elements into where a satellite appeared from the ground.

Everything here is topocentric: altitude and azimuth as seen from the observer,
not from the centre of the Earth. That is the frame the photograph is in.

SGP4, which skyfield uses, is only as good as the elements fed to it, and its
accuracy decays away from the element set's epoch — of order a kilometre per day
for a low orbit, which at a few hundred kilometres' range is a visible angular
error. Predictions are therefore tagged with the element age rather than being
presented as equally trustworthy regardless of it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from skyfield.api import EarthSatellite, load, wgs84

from satstreak.catalog import TleRecord
from satstreak.types import Observation

#: Elements older than this produce predictions too coarse to match a streak
#: against. Flagged rather than refused, since the caller may still want them.
STALE_ELEMENT_AGE = timedelta(days=7)


@dataclass(frozen=True)
class LookAngles:
    """Where a satellite was in the observer's sky at one instant."""

    altitude_deg: float
    """Degrees above the horizon. Negative means below it, and so unphotographable."""

    azimuth_deg: float
    """Degrees clockwise from north."""

    range_km: float
    """Distance from observer to satellite."""

    @property
    def is_above_horizon(self) -> bool:
        return self.altitude_deg > 0.0

    def separation_deg(self, other: LookAngles) -> float:
        """Angle between two directions in the sky.

        Naive subtraction of azimuths is wrong away from the horizon: one degree
        of azimuth spans a much smaller angle at high altitude than at low, and
        vanishes entirely at the zenith. This uses the spherical law of cosines.
        """
        alt_a, alt_b = math.radians(self.altitude_deg), math.radians(other.altitude_deg)
        delta_az = math.radians(self.azimuth_deg - other.azimuth_deg)
        cosine = math.sin(alt_a) * math.sin(alt_b) + math.cos(alt_a) * math.cos(alt_b) * math.cos(
            delta_az
        )
        return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


@dataclass(frozen=True)
class Track:
    """A satellite's path across the sky during one exposure."""

    norad_id: int
    name: str
    samples: tuple[LookAngles, ...]
    element_age: timedelta

    @property
    def is_visible(self) -> bool:
        """True when any part of the path was above the horizon."""
        return any(s.is_above_horizon for s in self.samples)

    @property
    def elements_are_stale(self) -> bool:
        return self.element_age > STALE_ELEMENT_AGE

    @property
    def arc_deg(self) -> float:
        """How far the satellite moved across the sky during the exposure."""
        if len(self.samples) < 2:
            return 0.0
        return self.samples[0].separation_deg(self.samples[-1])


class Propagator:
    """Propagates a catalogue of element sets to observer-relative positions."""

    def __init__(self, records: Sequence[TleRecord]) -> None:
        self._timescale = load.timescale()
        self._satellites: dict[int, EarthSatellite] = {}
        self._names: dict[int, str] = {}
        self._epochs: dict[int, datetime] = {}
        for record in records:
            satellite = EarthSatellite(record.line1, record.line2, record.name, self._timescale)
            self._satellites[record.norad_id] = satellite
            self._names[record.norad_id] = record.name
            self._epochs[record.norad_id] = record.epoch

    def __len__(self) -> int:
        return len(self._satellites)

    @property
    def norad_ids(self) -> tuple[int, ...]:
        return tuple(self._satellites)

    def name_of(self, norad_id: int) -> str:
        return self._names[norad_id]

    def _site(self, observation: Observation):
        return wgs84.latlon(
            observation.latitude_deg,
            observation.longitude_deg,
            elevation_m=observation.elevation_m,
        )

    def look_angles(
        self, norad_id: int, observation: Observation, when: datetime | None = None
    ) -> LookAngles:
        """Where one satellite was, as seen from the observer."""
        satellite = self._satellites[norad_id]
        moment = self._timescale.from_datetime(when or observation.utc)
        altitude, azimuth, distance = (satellite - self._site(observation)).at(moment).altaz()
        return LookAngles(
            altitude_deg=float(altitude.degrees),
            azimuth_deg=float(azimuth.degrees),
            range_km=float(distance.km),
        )

    def track(
        self,
        norad_id: int,
        observation: Observation,
        samples: int = 5,
    ) -> Track:
        """Sample one satellite's path across the exposure.

        The exposure is what turns a point into a streak, so a track is sampled
        over the shutter interval rather than evaluated at a single instant. With
        no exposure recorded the track collapses to one sample, which is honest:
        without knowing how long the shutter was open there is no way to say how
        far anything moved.
        """
        if samples < 1:
            raise ValueError("samples must be at least 1")
        start = observation.utc
        duration = observation.exposure_s
        if duration is None or samples == 1:
            moments = [start]
        else:
            step = duration / (samples - 1)
            moments = [start + timedelta(seconds=step * i) for i in range(samples)]

        return Track(
            norad_id=norad_id,
            name=self._names[norad_id],
            samples=tuple(self.look_angles(norad_id, observation, m) for m in moments),
            element_age=start - self._epochs[norad_id],
        )

    def above_horizon(
        self,
        observation: Observation,
        *,
        min_altitude_deg: float = 10.0,
        norad_ids: Iterable[int] | None = None,
    ) -> list[int]:
        """Which satellites were above the horizon at the shutter time.

        The default cut is 10 degrees rather than 0. Below that a satellite is
        behind terrain or buildings for almost any real observer, and is seen
        through enough atmosphere to be dimmed out of a photograph — so including
        it adds candidates that could not have made the streak.

        This is a coarse first pass only. It does not ask whether the satellite
        was sunlit or bright enough to record, which is what actually decides
        whether it left a trail.
        """
        candidates = self._satellites if norad_ids is None else norad_ids
        return [
            norad_id
            for norad_id in candidates
            if self.look_angles(norad_id, observation).altitude_deg >= min_altitude_deg
        ]
