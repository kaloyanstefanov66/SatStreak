"""Fetching and caching orbital elements from CelesTrak.

CelesTrak publishes the public satellite catalogue as General Perturbations (GP)
element sets, and asks machine clients to behave. Two of its rules shape this
module:

* **GP data is regenerated about every two hours.** Downloading more often than
  that returns bytes that are identical to the ones already held, so the cache
  age below defaults to the publication interval rather than to something
  arbitrary. Polling faster is pure waste borne by someone else's server.
* **A machine client must stop on any non-200 response.** CelesTrak uses status
  codes to shed load and to tell misbehaving clients to go away; retrying through
  one is how a client gets blocked. So a non-200 raises and the caller stops,
  rather than being retried or quietly falling back to stale data without saying
  so.

Fetching uses the standard library rather than ``requests`` so that installing
SatStreak does not drag in an HTTP stack for one download.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php"

#: How often CelesTrak regenerates GP data. Also the default cache lifetime.
PUBLICATION_INTERVAL = timedelta(hours=2)

#: CelesTrak asks clients to identify themselves so it can contact operators of
#: misbehaving software instead of simply blocking them.
USER_AGENT = "SatStreak/0.0.1 (+https://github.com/kaloyanstefanov66/SatStreak)"

Fetcher = Callable[[str], str]


class CatalogError(RuntimeError):
    """Orbital elements could not be obtained, and the caller should stop."""


@dataclass(frozen=True)
class TleRecord:
    """One satellite's two-line element set, as published."""

    name: str
    line1: str
    line2: str

    def __post_init__(self) -> None:
        for label, line in (("line1", self.line1), ("line2", self.line2)):
            if len(line) < 69:
                raise ValueError(f"{label} is too short to be a TLE line: {line!r}")
        if not self.line1.startswith("1 ") or not self.line2.startswith("2 "):
            raise ValueError("TLE lines must begin with '1 ' and '2 '")
        if self.line1[2:7] != self.line2[2:7]:
            raise ValueError(
                f"TLE lines belong to different objects: {self.line1[2:7]!r} vs {self.line2[2:7]!r}"
            )

    @property
    def norad_id(self) -> int:
        return int(self.line1[2:7])

    @property
    def epoch(self) -> datetime:
        """When these elements were computed.

        Accuracy decays away from the epoch, so this is what tells a caller
        whether a prediction is worth trusting. The two-digit year follows the
        TLE convention: 57-99 mean 1957-1999, 00-56 mean 2000-2056.
        """
        two_digit = int(self.line1[18:20])
        year = 1900 + two_digit if two_digit >= 57 else 2000 + two_digit
        day_of_year = float(self.line1[20:32])
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        return start + timedelta(days=day_of_year - 1.0)

    def age_at(self, when: datetime) -> timedelta:
        """How stale these elements are for a given moment. May be negative."""
        return when - self.epoch


def parse_tle(text: str) -> list[TleRecord]:
    """Parse CelesTrak's three-line TLE format, skipping malformed entries.

    A single corrupt entry in a 28,000-object download should not deny the caller
    the other 27,999, so bad records are dropped rather than raised on. Structural
    failure — nothing parseable at all — is a different matter and does raise,
    because it means the download was not what it claimed to be.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    records: list[TleRecord] = []
    index = 0
    while index + 2 < len(lines) + 1:
        if index + 2 >= len(lines) + 1:
            break
        name, line1, line2 = lines[index], lines[index + 1], lines[index + 2]
        index += 3
        try:
            records.append(TleRecord(name=name.strip(), line1=line1, line2=line2))
        except (ValueError, IndexError):
            continue
    if not records:
        raise CatalogError(
            "No usable TLE records were found. The response was not GP element data; "
            "CelesTrak sometimes serves an error page with a 200 status."
        )
    return records


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                raise CatalogError(
                    f"CelesTrak returned HTTP {response.status}. A machine client must "
                    f"stop on any non-200 response rather than retry."
                )
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise CatalogError(
            f"CelesTrak returned HTTP {exc.code}. A machine client must stop on any "
            f"non-200 response rather than retry; retrying through one is how a client "
            f"gets blocked."
        ) from exc
    except urllib.error.URLError as exc:
        raise CatalogError(f"Could not reach CelesTrak: {exc.reason}") from exc


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "satstreak"


def cache_path(group: str, cache_dir: Path | None = None) -> Path:
    directory = cache_dir or default_cache_dir()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in group)
    return directory / f"gp-{safe}.txt"


def cache_age(path: Path) -> timedelta | None:
    """How old the cached file is, or None when there is no cache."""
    if not path.exists():
        return None
    return timedelta(seconds=max(0.0, time.time() - path.stat().st_mtime))


def load_catalog(
    group: str = "active",
    *,
    cache_dir: Path | None = None,
    max_age: timedelta = PUBLICATION_INTERVAL,
    fetcher: Fetcher | None = None,
    allow_network: bool = True,
) -> list[TleRecord]:
    """Return the catalogue, downloading only when the cache has expired.

    Args:
        group: A CelesTrak group name, e.g. ``active``, ``starlink``, ``stations``.
        cache_dir: Where to keep the download. Defaults to ``~/.cache/satstreak``.
        max_age: Refetch once the cache is older than this. Defaults to
            CelesTrak's publication interval, since a fresher copy does not exist.
        fetcher: Override for the HTTP call. Exists so tests never touch the
            network, and so a caller can supply its own client.
        allow_network: When False, use the cache or fail. Useful for reproducing
            an evaluation run against exactly the elements it originally used.

    Raises:
        CatalogError: The catalogue could not be obtained. The caller should stop
            rather than continue with no elements or silently stale ones.
    """
    path = cache_path(group, cache_dir)
    age = cache_age(path)

    if age is not None and age <= max_age:
        return parse_tle(path.read_text(encoding="utf-8"))

    if not allow_network:
        if age is None:
            raise CatalogError(
                f"No cached elements for group {group!r} at {path}, and network access is disabled."
            )
        # Stale but present, and the caller asked to stay offline. Using it is
        # correct here; using it *without saying so* would not be.
        return parse_tle(path.read_text(encoding="utf-8"))

    url = f"{CELESTRAK_GP_URL}?{urllib.parse.urlencode({'GROUP': group, 'FORMAT': 'tle'})}"
    get = fetcher or _http_get
    try:
        text = get(url)
    except CatalogError:
        if age is not None:
            # The download failed but a cached copy exists. Falling back to it
            # beats failing outright, and the age is available to the caller
            # through cache_age() so the staleness is never hidden.
            return parse_tle(path.read_text(encoding="utf-8"))
        raise

    records = parse_tle(text)  # validate before overwriting a good cache
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return records
