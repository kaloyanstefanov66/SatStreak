"""Recovering where the camera was aimed, from the stars in the frame.

This is the last thing the user has to supply by hand, and removing it is what
turns "tell the tool where you pointed" into "give the tool a photo".

The contract is deliberately narrow. A solver takes an image and returns a
`Pointing`, or says it could not. Nothing above it knows or cares how the answer
was reached, which is what lets a second backend be dropped in later — and there
will be a second one, because the backend here needs Linux or WSL while the rest
of SatStreak runs anywhere (see decision 11).

Two things make this problem easier here than for a general plate solver, and
both are used:

* **The field of view is already known**, from the lens's 35 mm-equivalent focal
  length in EXIF. A general solver must search every plate scale; this one can
  be handed a narrow bracket, which is the difference between a verdict in
  seconds and one in minutes.
* **The time and place are already known**, so it is known which half of the sky
  was above the horizon. That is not yet exploited but is recorded here because
  it is the obvious next lever if speed becomes a problem.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import numpy as np

from satstreak.geometry import Pointing
from satstreak.types import Observation


@dataclass(frozen=True)
class SolveResult:
    """Where the camera was aimed, or why that could not be worked out."""

    pointing: Pointing | None
    seconds: float
    stars_used: int = 0
    message: str = ""
    centre_ra_deg: float | None = None
    centre_dec_deg: float | None = None

    @property
    def solved(self) -> bool:
        return self.pointing is not None


class PlateSolver(Protocol):
    """Anything that can turn an image into a pointing."""

    def solve(
        self, image: np.ndarray, observation: Observation, fov_hint_deg: float | None = None
    ) -> SolveResult: ...


def equatorial_to_pointing(
    ra_deg: float,
    dec_deg: float,
    scale_arcsec_per_px: float,
    orientation_deg: float,
    observation: Observation,
    width_px: int,
    height_px: int,
) -> Pointing:
    """Turn a plate solve's equatorial answer into this project's horizontal one.

    A solve reports right ascension and declination, which is where the camera
    pointed on the celestial sphere. SatStreak works in altitude and azimuth,
    because that is the frame satellites live in and because a tripod is aimed at
    a patch of sky above the observer rather than at a patch of sky chart. The
    conversion needs the time and the observer's latitude, both of which are
    already known.
    """
    from skyfield.api import load

    moment = load.timescale().from_datetime(observation.utc)
    local_sidereal_deg = (moment.gast * 15.0 + observation.longitude_deg) % 360.0

    hour_angle = math.radians((local_sidereal_deg - ra_deg) % 360.0)
    declination = math.radians(dec_deg)
    latitude = math.radians(observation.latitude_deg)

    sin_altitude = math.sin(declination) * math.sin(latitude) + math.cos(declination) * math.cos(
        latitude
    ) * math.cos(hour_angle)
    altitude = math.degrees(math.asin(max(-1.0, min(1.0, sin_altitude))))
    azimuth = (
        math.degrees(
            math.atan2(
                -math.cos(declination) * math.sin(hour_angle),
                math.sin(declination) * math.cos(latitude)
                - math.cos(declination) * math.sin(latitude) * math.cos(hour_angle),
            )
        )
        % 360.0
    )

    return Pointing(
        altitude_deg=altitude,
        azimuth_deg=azimuth,
        # The solver's orientation is measured on the sky, against north. Roll
        # here is measured against the zenith, and the two differ by the parallactic
        # angle. Rather than derive it, the roll search in `scan` refines this;
        # starting from the solver's value gives it a close first guess.
        roll_deg=orientation_deg % 180.0,
        scale_arcsec_per_px=scale_arcsec_per_px,
        width_px=width_px,
        height_px=height_px,
    )


class AstrometryNetSolver:
    """Plate solving through the `astrometry` package, which wraps Astrometry.net.

    Needs Linux or macOS; on Windows it requires WSL. That is why this sits
    behind the `PlateSolver` protocol rather than being called directly.

    Index files are downloaded on first use. The wide-field Tycho-2 series is
    0.36 GB for every scale, which is the one that matters for photographs; the
    deep Gaia series run to tens of gigabytes and are for telescopes.
    """

    def __init__(
        self,
        cache_directory: str,
        *,
        scales: set[int] | None = None,
        timeout_s: float = 90.0,
        max_stars: int = 120,
    ) -> None:
        self.cache_directory = cache_directory
        # Restricting scales is the single largest speed lever: one scale returns
        # a verdict in seconds where loading every scale can run for minutes
        # without returning one at all.
        self.scales = scales
        self.timeout_s = timeout_s
        self.max_stars = max_stars

    def extract_stars(self, image: np.ndarray) -> list[list[float]]:
        """Star-like sources, brightest first.

        Elongated detections are rejected. On these images the elongated things
        are the satellite trails themselves, and handing a trail to the solver as
        though it were a star corrupts the very geometry being recovered.
        """
        from scipy import ndimage

        data = image.astype(np.float32)
        box = max(8, min(64, min(data.shape) // 8))
        residual = data - ndimage.uniform_filter(data, size=box)

        median = float(np.median(residual))
        noise = float(np.median(np.abs(residual - median))) * 1.4826 or 1.0
        mask = residual > 5.0 * noise

        labels, count = ndimage.label(mask)
        if count == 0:
            return []

        found: list[tuple[float, float, float]] = []
        for index, region in enumerate(ndimage.find_objects(labels), start=1):
            window = labels[region] == index
            area = int(window.sum())
            if not 2 <= area <= 400:
                continue
            offset_y, offset_x = region[0].start, region[1].start
            local = np.argwhere(window)
            weights = residual[region][window]
            total = float(weights.sum())
            if total <= 0:
                continue
            ys = local[:, 0] + offset_y
            xs = local[:, 1] + offset_x
            centre_x = float((xs * weights).sum() / total)
            centre_y = float((ys * weights).sum() / total)

            dx, dy = xs - centre_x, ys - centre_y
            mu_xx = float((dx * dx * weights).sum() / total)
            mu_yy = float((dy * dy * weights).sum() / total)
            mu_xy = float((dx * dy * weights).sum() / total)
            common = math.sqrt(max(0.0, (mu_xx - mu_yy) ** 2 + 4.0 * mu_xy**2))
            major = math.sqrt(max(1e-9, (mu_xx + mu_yy + common) / 2.0))
            minor = math.sqrt(max(1e-9, (mu_xx + mu_yy - common) / 2.0))
            if major / minor > 2.5:
                continue  # a trail, not a star

            found.append((total, centre_x, centre_y))

        found.sort(reverse=True)
        return [[x, y] for _, x, y in found[: self.max_stars]]

    def solve(
        self, image: np.ndarray, observation: Observation, fov_hint_deg: float | None = None
    ) -> SolveResult:
        import time

        started = time.monotonic()
        try:
            import astrometry
        except ImportError:
            return SolveResult(
                pointing=None,
                seconds=0.0,
                message=(
                    "The astrometry package is not installed. It needs Linux or macOS; "
                    "on Windows it requires WSL."
                ),
            )

        stars = self.extract_stars(image)
        if len(stars) < 10:
            return SolveResult(
                pointing=None,
                seconds=round(time.monotonic() - started, 1),
                stars_used=len(stars),
                message=(
                    f"Only {len(stars)} star-like sources were found, which is too few to "
                    "solve. The sky may be too bright, the exposure too short, or the "
                    "frame mostly foreground."
                ),
            )

        height, width = image.shape
        size_hint = None
        if fov_hint_deg:
            centre = fov_hint_deg * 3600.0 / width
            # A generous bracket: a hint that excludes the true scale is worse
            # than no hint at all, and the EXIF focal length is only nominal.
            size_hint = astrometry.SizeHint(
                lower_arcsec_per_pixel=centre * 0.7, upper_arcsec_per_pixel=centre * 1.4
            )

        deadline = started + self.timeout_s

        def stop_when_done(logodds: list[float]) -> astrometry.Action:
            if time.monotonic() > deadline or (logodds and max(logodds) > 100.0):
                return astrometry.Action.STOP
            return astrometry.Action.CONTINUE

        solver = astrometry.Solver(
            astrometry.series_4100.index_files(
                cache_directory=self.cache_directory, scales=self.scales
            )
        )
        try:
            solution = solver.solve(
                stars=stars,
                size_hint=size_hint,
                position_hint=None,
                solution_parameters=astrometry.SolutionParameters(logodds_callback=stop_when_done),
            )
        finally:
            solver.close()

        elapsed = round(time.monotonic() - started, 1)
        if not solution.has_match():
            return SolveResult(
                pointing=None,
                seconds=elapsed,
                stars_used=len(stars),
                message=(
                    f"No match against the star catalogue, using {len(stars)} sources. "
                    "Wide fields are hardest: lens distortion grows away from centre and "
                    "the tangent-plane model does not account for it."
                ),
            )

        match = solution.best_match()
        pointing = equatorial_to_pointing(
            ra_deg=float(match.center_ra_deg),
            dec_deg=float(match.center_dec_deg),
            scale_arcsec_per_px=float(match.scale_arcsec_per_pixel),
            orientation_deg=0.0,
            observation=observation,
            width_px=width,
            height_px=height,
        )
        return SolveResult(
            pointing=pointing,
            seconds=elapsed,
            stars_used=len(stars),
            centre_ra_deg=float(match.center_ra_deg),
            centre_dec_deg=float(match.center_dec_deg),
            message=f"Solved from {len(stars)} stars in {elapsed}s.",
        )


def utc_now() -> datetime:  # pragma: no cover - convenience for callers
    from datetime import timezone

    return datetime.now(timezone.utc)
