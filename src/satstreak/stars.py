"""Bright stars, for rendering realistic frames and for checking pointing.

Satellites are the subject, but stars are what makes a photograph solvable, so
synthetic frames need real ones. A frame full of invented stars can exercise the
detector — they are distractors either way — but it cannot be plate-solved, and
a harness that cannot test the solver cannot test the part most likely to fail.

The source is the Hipparcos catalogue, filtered to naked-eye brightness. Two
practical reasons for that cut rather than something deeper: a phone photograph
records perhaps magnitude 4 in a city and 6 under a dark sky, so anything fainter
would not appear; and the whole sky to magnitude 6.5 is about nine thousand
stars, which is a file small enough to keep beside the code rather than a
download to manage.

The catalogue is fetched once and a compact subset cached, because the published
file is fifty megabytes of fixed-width text and all but four of its fields are
irrelevant here.
"""

from __future__ import annotations

import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np

from satstreak.propagate import LookAngles
from satstreak.types import Observation

HIPPARCOS_URL = "https://cdsarc.cds.unistra.fr/ftp/cats/I/239/hip_main.dat"

#: Naked-eye limit under a dark sky. Fainter stars would not appear in the
#: photographs this tool is for.
DEFAULT_MAGNITUDE_LIMIT = 6.5

#: The derived subset does not change, so it never needs refreshing the way
#: orbital elements do.
CACHE_LIFETIME = timedelta(days=3650)

USER_AGENT = "SatStreak/0.0.1 (+https://github.com/kaloyanstefanov66/SatStreak)"

Fetcher = Callable[[str], str]


class StarCatalogError(RuntimeError):
    """Star positions could not be obtained."""


@dataclass(frozen=True)
class Star:
    """One catalogue star, at its J2000 position."""

    hip: int
    ra_deg: float
    dec_deg: float
    magnitude: float

    @property
    def relative_brightness(self) -> float:
        """Linear flux relative to a magnitude 0 star.

        Magnitudes are logarithmic and backwards, so rendering with them
        directly would make every star look the same. Two stars five magnitudes
        apart differ in brightness by a factor of a hundred.
        """
        return 10.0 ** (-0.4 * self.magnitude)


def parse_hipparcos(text: str, magnitude_limit: float = DEFAULT_MAGNITUDE_LIMIT) -> list[Star]:
    """Pull the four useful fields out of the published catalogue.

    The file is pipe-delimited with a fixed column order: identifier at index 1,
    visual magnitude at 5, and J2000 right ascension and declination in degrees
    at 8 and 9. Rows with missing astrometry exist and are skipped rather than
    guessed at.
    """
    stars: list[Star] = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 10:
            continue
        try:
            magnitude = float(parts[5])
            if magnitude > magnitude_limit:
                continue
            stars.append(
                Star(
                    hip=int(parts[1]),
                    ra_deg=float(parts[8]),
                    dec_deg=float(parts[9]),
                    magnitude=magnitude,
                )
            )
        except (ValueError, IndexError):
            continue  # a row without usable astrometry

    if not stars:
        raise StarCatalogError(
            "No usable stars were parsed. The response was not the Hipparcos catalogue."
        )
    stars.sort(key=lambda s: s.magnitude)
    return stars


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "satstreak"


def cache_path(magnitude_limit: float, cache_dir: Path | None = None) -> Path:
    directory = cache_dir or default_cache_dir()
    return directory / f"stars-mag{magnitude_limit:g}.csv"


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read().decode("latin-1", errors="replace")
    except urllib.error.URLError as exc:
        raise StarCatalogError(f"Could not fetch the star catalogue: {exc}") from exc


def load_stars(
    magnitude_limit: float = DEFAULT_MAGNITUDE_LIMIT,
    *,
    cache_dir: Path | None = None,
    fetcher: Fetcher | None = None,
) -> list[Star]:
    """Return bright stars, downloading the catalogue only once."""
    path = cache_path(magnitude_limit, cache_dir)
    if path.exists():
        rows = path.read_text(encoding="utf-8").splitlines()
        stars = []
        for row in rows[1:]:
            hip, ra, dec, mag = row.split(",")
            stars.append(
                Star(hip=int(hip), ra_deg=float(ra), dec_deg=float(dec), magnitude=float(mag))
            )
        if stars:
            return stars

    get = fetcher or _http_get
    stars = parse_hipparcos(get(HIPPARCOS_URL), magnitude_limit)

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["hip,ra_deg,dec_deg,magnitude"]
    lines.extend(f"{s.hip},{s.ra_deg:.6f},{s.dec_deg:.6f},{s.magnitude:.3f}" for s in stars)
    path.write_text("\n".join(lines), encoding="utf-8")
    return stars


def stars_above_horizon(
    stars: list[Star],
    observation: Observation,
    *,
    min_altitude_deg: float = 0.0,
) -> list[tuple[Star, LookAngles]]:
    """Where each star was in the observer's sky, keeping those above the cut.

    Computed directly from sidereal time rather than through an ephemeris. A
    star is effectively at infinity, so its altitude and azimuth follow from the
    observer's latitude and the hour angle alone; going through skyfield's
    ephemeris route would mean downloading seventeen megabytes of planetary data
    to answer a question that does not involve any planets.

    Precession, nutation, aberration and refraction are all neglected, which
    costs at most an arcminute or two. At the plate scales this tool deals with
    — of order a hundred arcseconds per pixel on a phone — that is well under a
    pixel, and it is nowhere near the dominant error.

    Vectorised, because this runs inside a search.
    """
    from skyfield.api import load

    if not stars:
        return []

    timescale = load.timescale()
    moment = timescale.from_datetime(observation.utc)

    # Local apparent sidereal time, in degrees. Skyfield gives Greenwich
    # sidereal time in hours without needing any ephemeris.
    local_sidereal_deg = (moment.gast * 15.0 + observation.longitude_deg) % 360.0

    ra = np.array([s.ra_deg for s in stars])
    dec = np.radians(np.array([s.dec_deg for s in stars]))
    latitude = math.radians(observation.latitude_deg)

    hour_angle = np.radians((local_sidereal_deg - ra) % 360.0)

    sin_alt = np.sin(dec) * math.sin(latitude) + np.cos(dec) * math.cos(latitude) * np.cos(
        hour_angle
    )
    altitudes = np.degrees(np.arcsin(np.clip(sin_alt, -1.0, 1.0)))

    # Azimuth measured clockwise from north.
    azimuths = (
        np.degrees(
            np.arctan2(
                -np.cos(dec) * np.sin(hour_angle),
                np.sin(dec) * math.cos(latitude)
                - np.cos(dec) * math.sin(latitude) * np.cos(hour_angle),
            )
        )
        % 360.0
    )

    visible: list[tuple[Star, LookAngles]] = []
    for star, alt, az in zip(stars, altitudes, azimuths, strict=True):
        if alt >= min_altitude_deg:
            visible.append(
                (
                    star,
                    LookAngles(
                        altitude_deg=float(alt),
                        azimuth_deg=float(az),
                        # Stars are effectively at infinity; the range exists
                        # only because LookAngles is shared with satellites.
                        range_km=math.inf,
                    ),
                )
            )
    return visible


def refresh_cache(
    magnitude_limit: float = DEFAULT_MAGNITUDE_LIMIT, cache_dir: Path | None = None
) -> Path:
    """Force a re-download. Useful once, and after that essentially never."""
    path = cache_path(magnitude_limit, cache_dir)
    if path.exists():
        path.unlink()
    started = time.monotonic()
    stars = load_stars(magnitude_limit, cache_dir=cache_dir)
    print(f"cached {len(stars)} stars to {path} in {time.monotonic() - started:.0f}s")
    return path
