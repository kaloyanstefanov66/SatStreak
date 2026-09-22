"""Finding trails in an image, without being told where to look.

The tool sweeps a photograph and reports what it finds, so this stage sets the
error budget for everything after it. A missed trail costs the user a result they
might not have known was there. A false trail costs them a confident wrong
answer they have no way to check. The second is worse, so the defaults here
favour precision over recall, and the thresholds are named rather than buried so
that the trade can be moved deliberately.

The method is unglamorous on purpose: subtract a coarse background, threshold,
label connected regions, and keep the ones whose second moments say they are long
and thin. Stars are round and are rejected by the same test that accepts trails.
Nothing here is tuned to a particular camera, which is the point -- a detector
fitted to one set of images tends to be fitted to that set's artefacts too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from satstreak.types import Streak

#: Detection threshold above the background noise. Five sigma is high for source
#: detection, and deliberately so: at three sigma a 12-megapixel frame produces
#: thousands of noise peaks, and a handful of them will align into something a
#: line test accepts.
THRESHOLD_SIGMA = 5.0

#: A trail must be at least this many times longer than it is wide. Stars sit
#: near 1, and even a slightly trailed star rarely passes 3.
MIN_ELONGATION = 4.0

#: Shorter than this and a trail is indistinguishable from a cosmic ray hit or a
#: pair of adjacent hot pixels.
MIN_LENGTH_PX = 25.0

#: Regions smaller than this are noise clumps regardless of shape.
MIN_AREA_PX = 12


@dataclass(frozen=True)
class DetectorSettings:
    """Everything the detector can be moved on, in one place.

    Grouped so an evaluation can sweep them and report how precision and recall
    trade off, rather than leaving the defaults as unexamined magic numbers.
    """

    threshold_sigma: float = THRESHOLD_SIGMA
    min_elongation: float = MIN_ELONGATION
    min_length_px: float = MIN_LENGTH_PX
    min_area_px: int = MIN_AREA_PX
    background_box_px: int = 64
    max_streaks: int = 25

    def to_dict(self) -> dict[str, float | int]:
        return {
            "threshold_sigma": self.threshold_sigma,
            "min_elongation": self.min_elongation,
            "min_length_px": self.min_length_px,
            "min_area_px": self.min_area_px,
            "background_box_px": self.background_box_px,
        }


def _noise_estimate(residual: np.ndarray) -> float:
    """Robust noise level, via the median absolute deviation.

    A plain standard deviation is inflated by the very sources being looked for,
    which raises the threshold in exactly the frames that have the most to find.
    """
    median = float(np.median(residual))
    mad = float(np.median(np.abs(residual - median)))
    scaled = mad * 1.4826
    return scaled if scaled > 0 else float(residual.std()) or 1.0


def _streak_from_region(
    coordinates: np.ndarray, weights: np.ndarray, settings: DetectorSettings
) -> Streak | None:
    """Turn one labelled region into a streak, or reject it.

    Uses intensity-weighted second moments. The principal axis gives the
    direction; the extreme projections onto it give the endpoints, which matters
    because a trail's endpoints are the measurement the matcher compares against,
    not its centroid.
    """
    if len(coordinates) < settings.min_area_px:
        return None

    ys = coordinates[:, 0].astype(np.float64)
    xs = coordinates[:, 1].astype(np.float64)
    total = float(weights.sum())
    if total <= 0:
        return None

    centre_x = float((xs * weights).sum() / total)
    centre_y = float((ys * weights).sum() / total)
    dx, dy = xs - centre_x, ys - centre_y

    mu_xx = float((dx * dx * weights).sum() / total)
    mu_yy = float((dy * dy * weights).sum() / total)
    mu_xy = float((dx * dy * weights).sum() / total)

    # Eigenvalues of the 2x2 covariance give the semi-axes.
    common = math.sqrt(max(0.0, (mu_xx - mu_yy) ** 2 + 4.0 * mu_xy**2))
    major = math.sqrt(max(1e-9, (mu_xx + mu_yy + common) / 2.0))
    minor = math.sqrt(max(1e-9, (mu_xx + mu_yy - common) / 2.0))
    if major / minor < settings.min_elongation:
        return None  # round enough to be a star

    angle = 0.5 * math.atan2(2.0 * mu_xy, mu_xx - mu_yy)
    axis_x, axis_y = math.cos(angle), math.sin(angle)
    projections = dx * axis_x + dy * axis_y
    low, high = float(projections.min()), float(projections.max())
    length = high - low
    if length < settings.min_length_px:
        return None

    peak = float(weights.max())
    return Streak(
        x1=centre_x + low * axis_x,
        y1=centre_y + low * axis_y,
        x2=centre_x + high * axis_x,
        y2=centre_y + high * axis_y,
        # Elongation past the threshold is the evidence that this is a trail, so
        # it is what the score reports. Uncalibrated, like the match score.
        detection_score=min(1.0, (major / minor) / (settings.min_elongation * 3.0)),
        signal_to_noise=peak,
    )


def detect_streaks(image: np.ndarray, settings: DetectorSettings | None = None) -> list[Streak]:
    """Find trails in a greyscale image, brightest first.

    Args:
        image: Two-dimensional array. Colour images should be converted to
            greyscale by the caller, which keeps this stage free of image-format
            concerns.

    Returns:
        Streaks ordered by detection score, highest first. An empty list means
        the frame was swept and nothing trail-like was found, which is a result
        and not a failure.
    """
    settings = settings or DetectorSettings()
    if image.ndim != 2:
        raise ValueError(f"expected a 2-D greyscale image, got shape {image.shape}")

    data = image.astype(np.float32)

    # A night photograph has strong large-scale structure: light pollution, the
    # Milky Way, vignetting. Thresholding without removing it finds the bright
    # part of the sky rather than the things in it.
    box = max(8, min(settings.background_box_px, min(data.shape) // 4))
    residual = data - ndimage.uniform_filter(data, size=box)

    noise = _noise_estimate(residual)
    mask = residual > settings.threshold_sigma * noise
    labels, count = ndimage.label(mask)
    if count == 0:
        return []

    found: list[Streak] = []
    for index, region in enumerate(ndimage.find_objects(labels), start=1):
        window = labels[region] == index
        if int(window.sum()) < settings.min_area_px:
            continue
        offset_y, offset_x = region[0].start, region[1].start
        local = np.argwhere(window)
        coordinates = local + np.array([offset_y, offset_x])
        weights = residual[region][window]
        streak = _streak_from_region(coordinates, weights, settings)
        if streak is not None:
            found.append(streak)

    found.sort(key=lambda s: s.detection_score, reverse=True)
    return found[: settings.max_streaks]
