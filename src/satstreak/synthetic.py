"""Generating night-sky frames with a known right answer.

Real photographs are the eventual test, but they cannot validate the matcher on
their own: when a match comes back wrong there is no way to tell whether the
detector mislocated the trail, the matcher scored it badly, or the plate solve
gave the wrong pointing. A synthetic frame fixes every one of those, because the
pointing, the satellite and the trail's exact pixel path are all inputs.

The satellites are real. Tracks come from actual orbital elements propagated by
SGP4, so a frame generated from an ISS pass contains the trail the ISS would
really have drawn. Only the camera and the stars are invented.

What this deliberately does **not** simulate: lens distortion, sky gradients from
light pollution, star trailing from a moving mount, cosmic ray hits, cloud, or
foreground. Those are what make real photographs hard, and a detector tuned only
against these frames will look better than it is. The frames exist to prove the
geometry and matching are right, not to stand in for reality.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from satstreak.geometry import PixelTrack, Pointing
from satstreak.stars import Star
from satstreak.types import Observation


@dataclass(frozen=True)
class TruthTrail:
    """Where a satellite really was in a generated frame."""

    norad_id: int
    name: str
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def length_px(self) -> float:
        return float(np.hypot(self.x2 - self.x1, self.y2 - self.y1))

    @property
    def angle_deg(self) -> float:
        """Orientation in [0, 180), matching the detected-streak convention."""
        return float(np.degrees(np.arctan2(self.y2 - self.y1, self.x2 - self.x1)) % 180.0)


@dataclass(frozen=True)
class SyntheticFrame:
    """A generated image and everything that is true about it."""

    image: np.ndarray
    """Greyscale, uint8, shape (height, width)."""

    observation: Observation
    pointing: Pointing
    truth: tuple[TruthTrail, ...]
    """One entry per satellite whose trail was drawn. Empty for an empty sky."""

    def truth_for(self, norad_id: int) -> TruthTrail | None:
        return next((t for t in self.truth if t.norad_id == norad_id), None)


def _stamp_gaussian(image: np.ndarray, x: float, y: float, amplitude: float, sigma: float) -> None:
    """Add a Gaussian spot, clipped to the frame.

    Stars and trails are both built from these. A trail is simply a dense line of
    them, which gives it the same cross-section a real one has rather than the
    hard edges of a drawn line.
    """
    height, width = image.shape
    radius = max(1, int(np.ceil(3.0 * sigma)))
    x0, x1 = max(0, int(x) - radius), min(width, int(x) + radius + 1)
    y0, y1 = max(0, int(y) - radius), min(height, int(y) + radius + 1)
    if x0 >= x1 or y0 >= y1:
        return

    xs = np.arange(x0, x1, dtype=np.float32) - x
    ys = np.arange(y0, y1, dtype=np.float32) - y
    squared = ys[:, None] ** 2 + xs[None, :] ** 2
    image[y0:y1, x0:x1] += amplitude * np.exp(-squared / (2.0 * sigma**2))


#: Stamps per pixel along a trail. Dense enough that the ridge is continuous.
_STAMPS_PER_PIXEL = 2.0


def draw_trail(
    image: np.ndarray,
    points: list[tuple[float, float]],
    peak_brightness: float,
    sigma: float,
) -> None:
    """Draw a trail whose ridge peaks at ``peak_brightness`` above the background.

    The normalisation matters more than it looks. A trail is built from Gaussian
    stamps walked along its path, and those stamps overlap and add: at two stamps
    per pixel with sigma 1.4, each pixel collects contributions from roughly
    seventeen of them, so a naive amplitude of 6 produces a ridge peaking near 42.
    Without this correction the brightness parameter means nothing physical, and
    any statement about the faintest detectable trail is measuring the wrong
    quantity.

    The ridge of overlapping Gaussians spaced ``d`` apart peaks at
    ``A * sqrt(2*pi) * sigma / d``, so the per-stamp amplitude is scaled by the
    inverse of that.
    """
    spacing = 1.0 / _STAMPS_PER_PIXEL
    accumulation = math.sqrt(2.0 * math.pi) * sigma / spacing
    amplitude = peak_brightness / accumulation

    for (ax, ay), (bx, by) in itertools.pairwise(points):
        span = float(np.hypot(bx - ax, by - ay))
        steps = max(2, int(span * _STAMPS_PER_PIXEL))
        for i in range(steps + 1):
            t = i / steps
            _stamp_gaussian(image, ax + (bx - ax) * t, ay + (by - ay) * t, amplitude, sigma)


def render_frame(
    observation: Observation,
    pointing: Pointing,
    tracks: list[PixelTrack],
    *,
    star_count: int = 250,
    real_stars: list[tuple[Star, Any]] | None = None,
    limiting_magnitude: float = 5.5,
    background: float = 12.0,
    noise_sigma: float = 3.0,
    star_sigma: float = 1.4,
    trail_sigma: float = 1.4,
    trail_brightness: float = 45.0,
    seed: int = 0,
) -> SyntheticFrame:
    """Render a frame containing the given satellite tracks.

    Args:
        tracks: Already projected into pixel space. Tracks that do not cross the
            frame are skipped and do not appear in the truth, so a caller can
            hand over everything `predict` returned without filtering first.
        real_stars: Catalogue stars with their sky positions, from
            `satstreak.stars.stars_above_horizon`. When given, these are drawn at
            their true places and `star_count` is ignored. This is what makes a
            generated frame plate-solvable, and therefore what lets the solver be
            tested against a known answer.
        limiting_magnitude: The faintest star drawn, and the one rendered just
            above the noise. Brighter stars scale up from there and saturate, as
            they do on a real sensor. Lowering it models a light-polluted sky.
        star_count: Invented stars, scattered uniformly, used only when
            `real_stars` is not given. They are distractors for the detector and
            their positions carry no astronomical meaning, so a frame built from
            them cannot be plate-solved.
        trail_brightness: Peak brightness of the trail's ridge above the
            background, in the same units as `noise_sigma`. The default is a
            comfortably bright trail; lowering it towards `noise_sigma` is how
            to find where a detector stops working.

    Returns:
        The frame, with truth entries for every trail actually drawn.
    """
    rng = np.random.default_rng(seed)
    height, width = pointing.height_px, pointing.width_px
    image = np.full((height, width), background, dtype=np.float32)

    if real_stars is not None:
        # Real stars at their real places. Brightness follows the magnitude
        # scale, which is logarithmic and inverted: a star five magnitudes
        # brighter delivers a hundred times the flux, so a linear reading of
        # magnitude would render every star identically.
        floor = 5.0 * noise_sigma
        for star, angles in real_stars:
            if star.magnitude > limiting_magnitude:
                continue
            point = pointing.project(angles)
            if point is None:
                continue
            amplitude = floor * 10.0 ** (-0.4 * (star.magnitude - limiting_magnitude))
            _stamp_gaussian(image, point.x, point.y, min(amplitude, 255.0 - background), star_sigma)
    else:
        for _ in range(star_count):
            # A steep brightness distribution, so a few stars dominate and most
            # are faint, which is closer to a real field than uniform brightness.
            amplitude = float(rng.pareto(1.8) * 25.0 + 10.0)
            _stamp_gaussian(
                image,
                float(rng.uniform(0, width)),
                float(rng.uniform(0, height)),
                amplitude,
                star_sigma,
            )

    truth: list[TruthTrail] = []
    for track in tracks:
        visible = track.visible_points
        if len(visible) < 2:
            continue
        points = [(p.x, p.y) for p in visible]
        draw_trail(image, points, trail_brightness, trail_sigma)
        truth.append(
            TruthTrail(
                norad_id=track.norad_id,
                name=track.name,
                x1=points[0][0],
                y1=points[0][1],
                x2=points[-1][0],
                y2=points[-1][1],
            )
        )

    image += rng.normal(0.0, noise_sigma, size=image.shape).astype(np.float32)
    return SyntheticFrame(
        image=np.clip(image, 0, 255).astype(np.uint8),
        observation=observation,
        pointing=pointing,
        truth=tuple(truth),
    )
