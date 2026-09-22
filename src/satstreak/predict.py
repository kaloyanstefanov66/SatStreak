"""Which satellites crossed a given patch of sky at a given moment.

This is prediction, not identification. It answers what *could* have been in a
photograph, and the answer is routinely dozens of objects: a phone frame covers
about 16% of the visible sky, which measurement against the live catalogue puts
at roughly a hundred catalogued objects at any moment.

It exists for two reasons. The matcher needs exactly this list internally, to
have candidates to score against a detected trail. And it is the view needed to
debug the matcher, because a matcher that finds nothing is indistinguishable from
one whose candidate list was empty all along.

It needs no plate solving. Given a pointing -- from a solve, or supplied by hand,
or synthesised for a test -- everything here works from orbital elements alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from satstreak.catalog import TleRecord, load_catalog
from satstreak.geometry import PixelTrack, Pointing
from satstreak.propagate import Propagator, Track
from satstreak.types import Observation

#: Below this a satellite is behind terrain or buildings for most real observers,
#: and is seen through enough atmosphere to be dimmed out of a photograph.
DEFAULT_MIN_ALTITUDE_DEG = 10.0


@dataclass(frozen=True)
class Prediction:
    """One satellite that was in the sky, with its path if a pointing was given."""

    track: Track
    pixels: PixelTrack | None = None

    @property
    def norad_id(self) -> int:
        return self.track.norad_id

    @property
    def name(self) -> str:
        return self.track.name

    @property
    def in_frame(self) -> bool:
        return self.pixels is not None and self.pixels.crosses_frame

    @property
    def peak_altitude_deg(self) -> float:
        return max(s.altitude_deg for s in self.track.samples)


def predict(
    observation: Observation,
    pointing: Pointing | None = None,
    *,
    records: list[TleRecord] | None = None,
    group: str = "active",
    min_altitude_deg: float = DEFAULT_MIN_ALTITUDE_DEG,
    samples: int = 5,
    propagator: Propagator | None = None,
) -> list[Prediction]:
    """Satellites above the horizon, and where they crossed the frame.

    Args:
        observation: When and where the photograph was taken.
        pointing: Where the camera was aimed. Without it the result is every
            satellite above ``min_altitude_deg``, which is useful on its own and
            is what the CLI reports when no aim is supplied.
        records: Pre-loaded elements, so a caller can avoid repeated downloads
            and so tests never touch the network.
        propagator: A pre-built propagator, for the same reason. Building one for
            16,000 objects is the slowest step, and reusing it across several
            photographs from the same session is worth the argument.

    Returns:
        Predictions ordered by peak altitude, highest first, since the highest
        are both the likeliest to have been photographed and the easiest to
        check by eye.

    Note:
        This does **not** filter on whether a satellite was sunlit or bright
        enough to record, which is what actually decides whether it left a trail.
        Everything geometrically present is returned, and the caller decides.
    """
    if propagator is None:
        propagator = Propagator(records if records is not None else load_catalog(group))

    visible = propagator.above_horizon(observation, min_altitude_deg=min_altitude_deg)

    predictions: list[Prediction] = []
    for norad_id in visible:
        track = propagator.track(norad_id, observation, samples=samples)
        pixels = pointing.project_track(track) if pointing is not None else None
        if pointing is not None and (pixels is None or not pixels.crosses_frame):
            continue  # above the horizon, but not in this photograph
        predictions.append(Prediction(track=track, pixels=pixels))

    predictions.sort(key=lambda p: p.peak_altitude_deg, reverse=True)
    return predictions
