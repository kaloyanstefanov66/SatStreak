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

#: Field widths to assume when the photograph records no focal length. A
#: bracket this wide still costs far less than searching every plate scale:
#: blind solving a single frame ran for minutes without returning, where a
#: bracketed one returns in about a second. The range spans a long lens to an
#: ultra-wide phone camera, which covers essentially every photograph a person
#: would point at the sky.
ASSUMED_FOV_RANGE_DEG = (12.0, 120.0)


#: Where index files live unless told otherwise.
def default_index_dir() -> str:
    from pathlib import Path as _Path

    return str(_Path.home() / ".cache" / "satstreak" / "astrometry")


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

    def _solve_in_child(self, stars, lower, upper, queue) -> None:  # pragma: no cover
        """Runs in a separate process so the parent can kill it on overrun."""
        import astrometry

        try:
            solver = astrometry.Solver(
                astrometry.series_4100.index_files(
                    cache_directory=self.cache_directory, scales=self.scales
                )
            )
            solution = solver.solve(
                stars=stars,
                size_hint=astrometry.SizeHint(
                    lower_arcsec_per_pixel=lower, upper_arcsec_per_pixel=upper
                ),
                position_hint=None,
                solution_parameters=astrometry.SolutionParameters(),
            )
            if not solution.has_match():
                queue.put((None, None))
                return
            match = solution.best_match()
            queue.put(
                (
                    None,
                    {
                        "ra": float(match.center_ra_deg),
                        "dec": float(match.center_dec_deg),
                        "scale": float(match.scale_arcsec_per_pixel),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported to the parent, not raised
            queue.put((f"{type(exc).__name__}: {exc}", None))

    def solve(
        self, image: np.ndarray, observation: Observation, fov_hint_deg: float | None = None
    ) -> SolveResult:
        import multiprocessing
        import time

        started = time.monotonic()
        try:
            import astrometry  # noqa: F401 - availability check only
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
        if fov_hint_deg:
            centre = fov_hint_deg * 3600.0 / width
            # A generous bracket: a hint that excludes the true scale is worse
            # than no hint at all, and the EXIF focal length is only nominal.
            lower, upper = centre * 0.7, centre * 1.4
        else:
            # Never search blind. Without any hint the solver tries every plate
            # scale, which is the difference between a second and several
            # minutes, and it is the mistake most likely to make the tool look
            # broken rather than slow.
            widest, narrowest = ASSUMED_FOV_RANGE_DEG[1], ASSUMED_FOV_RANGE_DEG[0]
            lower = narrowest * 3600.0 / width
            upper = widest * 3600.0 / width

        # The library exposes no timeout, and its logodds_callback is not a
        # substitute: it is only invoked when there are log-odds to report, so a
        # search grinding through quads without matching never notices the clock.
        # The only bound it cannot evade is killing the process.
        queue: multiprocessing.Queue = multiprocessing.Queue()
        child = multiprocessing.Process(
            target=self._solve_in_child, args=(stars, lower, upper, queue), daemon=True
        )
        child.start()
        child.join(self.timeout_s)
        if child.is_alive():
            child.terminate()
            child.join(5)
            return SolveResult(
                pointing=None,
                seconds=round(time.monotonic() - started, 1),
                stars_used=len(stars),
                message=(
                    f"Gave up after {self.timeout_s:.0f}s. Wide or distorted fields are "
                    "hardest, and a frame that is mostly foreground gives the solver "
                    "terrain rather than stars."
                ),
            )
        try:
            error, match = queue.get_nowait()
        except Exception:  # noqa: BLE001 - child died without reporting
            error, match = "the solver process died without a result", None

        elapsed = round(time.monotonic() - started, 1)
        if match is None:
            return SolveResult(
                pointing=None,
                seconds=elapsed,
                stars_used=len(stars),
                message=(
                    error
                    or f"No match against the star catalogue, using {len(stars)} sources. "
                    "Wide fields are hardest: lens distortion grows away from centre and "
                    "the tangent-plane model does not account for it."
                ),
            )

        pointing = equatorial_to_pointing(
            ra_deg=match["ra"],
            dec_deg=match["dec"],
            scale_arcsec_per_px=match["scale"],
            orientation_deg=0.0,
            observation=observation,
            width_px=width,
            height_px=height,
        )
        return SolveResult(
            pointing=pointing,
            seconds=elapsed,
            stars_used=len(stars),
            centre_ra_deg=match["ra"],
            centre_dec_deg=match["dec"],
            message=f"Solved from {len(stars)} stars in {elapsed}s.",
        )


def utc_now() -> datetime:  # pragma: no cover - convenience for callers
    from datetime import timezone

    return datetime.now(timezone.utc)
