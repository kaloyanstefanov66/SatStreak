#!/usr/bin/env python3
"""Milestone 0, part two: an upper bound on solve rate, from public photographs.

`platesolve_spike.py` answers the real question but needs photographs that do not
exist yet. This script gets a partial answer immediately, by pulling freely
licensed night-sky images from Wikimedia Commons and running them through the
same solver.

What this measures, and what it does not
----------------------------------------
Every reachable public corpus is selected for being good. A photograph on Commons
is one somebody chose to upload to an encyclopedia; nova.astrometry.net's gallery
shows only submissions that succeeded, and exposes no way to enumerate the ones
that failed. So the number this produces is an **upper bound**, not an estimate.

That asymmetry is the whole point. An upper bound can only give a confident
*negative*:

* If phone photographs here solve at 20%, stop. Photographs from a user standing
  in a Sofia street at midnight will be worse, and the premise has failed.
* If they solve at 85%, much less has been learned than it feels like, and the
  question still needs real photographs before milestones 4 and 5 are committed to.

The segmentation by camera class is what makes this worth running at all. An
aggregate solve rate is dominated by tracked DSLR astrophotography and says
nothing about phones; the phone subset, biased as it is, is at least about the
right population.

Usage
-----
Fetch a corpus and build the attribution manifest, uploading nothing::

    python spikes/corpus_spike.py --fetch-only

Fetch and solve (needs ASTROMETRY_API_KEY)::

    export ASTROMETRY_API_KEY=xxxxxxxx
    python spikes/corpus_spike.py --limit 40
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import requests
    from platesolve_spike import FULL_FRAME_WIDTH_MM, ExifFacts, Nova
except ImportError as exc:  # pragma: no cover - spike-only dependency
    sys.exit(f"Missing dependency ({exc}). Run: python -m pip install -e .[spike]")

COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Commons asks that API clients identify themselves. Anonymous scraping of their
# API is both rude and liable to be throttled.
USER_AGENT = "SatStreak-research/0.1 (https://github.com/kaloyanstefanov66/SatStreak)"

#: Searches chosen to span the range of night photography, not just the pretty end.
#: The satellite-specific queries double as a source of matcher ground truth.
SEARCHES = [
    "night sky stars",
    "starry sky long exposure",
    "milky way night photograph",
    "night sky city light pollution",
    "ISS pass night sky trail",
    "Starlink satellites train sky",
    "satellite trail night sky long exposure",
    "constellation night sky photograph",
]

#: Only licences that permit redistribution, so anything kept can ship as a
#: CI fixture. Attribution is recorded in the manifest either way.
FREE_LICENCES = ("cc0", "cc by", "cc-by", "public domain", "pd-", "attribution")

PHONE_HINTS = (
    "iphone",
    "ipad",
    "pixel",
    "galaxy",
    "sm-",
    "gt-",
    "redmi",
    "xiaomi",
    "mi ",
    "huawei",
    "honor",
    "oneplus",
    "moto",
    "motorola",
    "nokia",
    "oppo",
    "vivo",
    "poco",
    "realme",
    "lg-",
    "nexus",
    "sony xperia",
)
BIG_CAMERA_HINTS = (
    "eos",
    "nikon",
    "ilce",
    "dsc-",
    "slt-",
    "fujifilm",
    "x-t",
    "x-pro",
    "pentax",
    "olympus",
    "e-m",
    "dmc-",
    "dc-",
    "canon",
    "leica",
    "sigma",
    "d800",
    "d810",
    "d750",
    "d7000",
    "d5",
    "z 6",
    "z 7",
    "alpha",
)

#: Commons serves originals that can run to tens of megabytes. Solving a
#: downscaled copy is faster and is what the solver would see from a phone
#: anyway -- but downscaling also suppresses noise, which biases the result
#: *upward*, so the width used is recorded alongside every result.
THUMBNAIL_WIDTH = 3000


@dataclass
class CorpusItem:
    """One Commons image, its provenance, and how it fared."""

    title: str
    page_url: str
    file_url: str
    licence: str
    artist: str
    credit: str
    camera_class: str
    exif: ExifFacts
    downscaled_to_px: int | None = None
    local_path: str | None = None
    solved: bool | None = None
    seconds: float | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    field_radius_deg: float | None = None
    job_url: str | None = None
    error: str | None = None
    names_a_satellite: bool = False
    """True when the file title names a specific satellite, making it a candidate
    ground-truth case for the matcher rather than just a solve-rate data point."""


def _strip_html(value: Any) -> str:
    text = str(value or "")
    out, depth = [], 0
    for char in text:
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return " ".join("".join(out).split())


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return float(numerator) / float(denominator)
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def classify_camera(model: str | None) -> str:
    """Bucket a camera model. Crude on purpose; only the phone bucket matters."""
    if not model:
        return "unknown"
    lowered = model.lower()
    if any(hint in lowered for hint in PHONE_HINTS):
        return "phone"
    if any(hint in lowered for hint in BIG_CAMERA_HINTS):
        return "big_camera"
    return "unknown"


def exif_from_commons(meta: dict[str, Any]) -> ExifFacts:
    """Build ExifFacts from the Commons API rather than from the file.

    This matters: Commons' thumbnailer strips EXIF, so the downloaded bytes carry
    no metadata at all. The API is the only place the original values survive.
    """
    facts = ExifFacts()
    make, model = meta.get("Make"), meta.get("Model")
    if make or model:
        facts.camera = " ".join(_strip_html(p) for p in (make, model) if p).strip()

    raw_time = meta.get("DateTimeOriginal") or meta.get("DateTime")
    if raw_time:
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                naive = datetime.strptime(_strip_html(raw_time)[:19], fmt)
            except ValueError:
                continue
            # Commons does not record the offset, so the clock is unanchored.
            # Flagged rather than silently treated as UTC.
            facts.timestamp_utc = naive.replace(tzinfo=timezone.utc).isoformat()
            facts.timestamp_is_guessed_tz = True
            break

    facts.exposure_s = _as_float(meta.get("ExposureTime"))
    facts.iso = int(iso) if (iso := _as_float(meta.get("ISOSpeedRatings"))) else None
    facts.focal_length_35mm = _as_float(meta.get("FocalLengthIn35mmFilm"))
    facts.latitude_deg = _as_float(meta.get("GPSLatitude"))
    facts.longitude_deg = _as_float(meta.get("GPSLongitude"))
    facts.altitude_m = _as_float(meta.get("GPSAltitude"))

    if facts.focal_length_35mm:
        half = FULL_FRAME_WIDTH_MM / (2.0 * facts.focal_length_35mm)
        facts.estimated_fov_width_deg = round(2.0 * math.degrees(math.atan(half)), 3)

    for name, value in [
        ("timestamp", facts.timestamp_utc),
        ("gps", facts.latitude_deg),
        ("exposure", facts.exposure_s),
        ("focal_length_35mm", facts.focal_length_35mm),
    ]:
        if value is None:
            facts.missing.append(name)
    return facts


def search_commons(session: requests.Session, query: str, limit: int) -> list[CorpusItem]:
    """Return usable candidates for one search term."""
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": "6",
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|metadata|size",
        "iiurlwidth": str(THUMBNAIL_WIDTH),
    }
    response = session.get(COMMONS_API, params=params, timeout=45)
    response.raise_for_status()
    pages = (response.json().get("query") or {}).get("pages") or {}

    items: list[CorpusItem] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        if not info:
            continue
        meta = {m["name"]: m.get("value") for m in (info.get("metadata") or [])}
        ext = info.get("extmetadata") or {}
        licence = _strip_html(ext.get("LicenseShortName", {}).get("value", ""))

        if not any(token in licence.lower() for token in FREE_LICENCES):
            continue

        exif = exif_from_commons(meta)
        # No timestamp means the image cannot ever become a real test case, only
        # a solve-rate tick. Still useful, but drop the ones with nothing at all.
        if exif.timestamp_utc is None and exif.exposure_s is None:
            continue

        title = page.get("title", "")
        lowered = title.lower()
        items.append(
            CorpusItem(
                title=title,
                page_url=info.get("descriptionurl", ""),
                file_url=info.get("thumburl") or info.get("url", ""),
                licence=licence,
                artist=_strip_html(ext.get("Artist", {}).get("value", "")),
                credit=_strip_html(ext.get("Credit", {}).get("value", "")),
                camera_class=classify_camera(exif.camera),
                exif=exif,
                downscaled_to_px=THUMBNAIL_WIDTH if info.get("thumburl") else None,
                names_a_satellite=any(
                    n in lowered for n in ("iss ", "starlink", "tiangong", "hubble", "satellite")
                ),
            )
        )
    return items


def gather(session: requests.Session, per_query: int, limit: int) -> list[CorpusItem]:
    """Collect candidates across every search term, fairly.

    Draining the searches in order starves the ones at the end, and the
    satellite-specific terms are last precisely because they are narrowest. Those
    are also the only source of matcher ground truth, so the take is round-robin:
    one image from each query in turn until the limit is reached.
    """
    buckets: list[list[CorpusItem]] = []
    for query in SEARCHES:
        try:
            found = search_commons(session, query, per_query)
        except requests.RequestException as exc:
            print(f"  ! search failed for {query!r}: {exc}")
            found = []
        print(f"  {query:<48} {len(found)} usable")
        buckets.append(found)
        time.sleep(0.5)  # be polite to the Commons API

    seen: set[str] = set()
    collected: list[CorpusItem] = []
    for rank in range(max((len(b) for b in buckets), default=0)):
        for bucket in buckets:
            if len(collected) >= limit:
                return collected
            if rank < len(bucket) and bucket[rank].title not in seen:
                seen.add(bucket[rank].title)
                collected.append(bucket[rank])
    return collected


def download(session: requests.Session, item: CorpusItem, into: Path) -> bool:
    into.mkdir(parents=True, exist_ok=True)
    name = item.title.removeprefix("File:").replace("/", "_")
    if not name.lower().endswith((".jpg", ".jpeg", ".png")):
        name += ".jpg"
    destination = into / name
    try:
        response = session.get(item.file_url, timeout=120)
        response.raise_for_status()
        destination.write_bytes(response.content)
    except requests.RequestException as exc:
        item.error = f"download failed: {exc}"
        return False
    item.local_path = str(destination)
    return True


def solve(nova: Nova, item: CorpusItem, budget_s: float) -> None:
    started = time.monotonic()
    try:
        blob = Path(item.local_path or "").read_bytes()
        subid = nova.submit(
            Path(item.local_path or "x").name, blob, item.exif.estimated_fov_width_deg
        )
        solved, job_id = nova.wait(subid, budget_s)
        item.solved = solved
        item.seconds = round(time.monotonic() - started, 1)
        if job_id is not None:
            item.job_url = f"https://nova.astrometry.net/status/{job_id}"
        if solved and job_id is not None:
            cal = nova.calibration(job_id)
            item.ra_deg = cal.get("ra")
            item.dec_deg = cal.get("dec")
            item.field_radius_deg = cal.get("radius")
    except Exception as exc:  # noqa: BLE001 - a spike records failures, it does not raise
        item.solved = False
        item.seconds = round(time.monotonic() - started, 1)
        item.error = f"{type(exc).__name__}: {exc}"


def summarise(items: list[CorpusItem]) -> dict[str, Any]:
    def rate(subset: list[CorpusItem]) -> dict[str, Any]:
        attempted = [i for i in subset if i.solved is not None]
        solved = [i for i in attempted if i.solved]
        times = sorted(i.seconds for i in solved if i.seconds is not None)
        return {
            "count": len(subset),
            "attempted": len(attempted),
            "solved": len(solved),
            "solve_rate_upper_bound": (
                round(len(solved) / len(attempted), 3) if attempted else None
            ),
            "median_seconds": times[len(times) // 2] if times else None,
        }

    by_class = {
        name: rate([i for i in items if i.camera_class == name])
        for name in ("phone", "big_camera", "unknown")
    }
    return {
        "caveat": (
            "These are UPPER BOUNDS. Wikimedia Commons images are self-selected for "
            "quality, and images were downscaled before solving, which suppresses "
            "noise. A low rate here is conclusive; a high rate here is not."
        ),
        "downscaled_to_px": THUMBNAIL_WIDTH,
        "overall": rate(items),
        "by_camera_class": by_class,
        "with_timestamp": sum(1 for i in items if i.exif.timestamp_utc),
        "with_gps": sum(1 for i in items if i.exif.latitude_deg is not None),
        "with_exposure": sum(1 for i in items if i.exif.exposure_s is not None),
        "ground_truth_candidates": sum(1 for i in items if i.names_a_satellite),
    }


def write_attribution(items: list[CorpusItem], path: Path) -> None:
    """CC-BY and CC-BY-SA require credit. This file is how the repo gives it."""
    lines = [
        "# Corpus attribution",
        "",
        "Images fetched from Wikimedia Commons by `spikes/corpus_spike.py`.",
        "Each is used under the licence shown. Author credit as recorded on Commons.",
        "",
        "| File | Author | Licence | Source |",
        "|------|--------|---------|--------|",
    ]
    for item in items:
        name = item.title.removeprefix("File:")
        lines.append(
            f"| {name} | {item.artist or 'unknown'} | {item.licence} | [Commons]({item.page_url}) |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upper-bound solve rate from public images.")
    parser.add_argument("--limit", type=int, default=30, help="max images to gather")
    parser.add_argument("--per-query", type=int, default=12, help="results per search term")
    parser.add_argument("--fetch-only", action="store_true", help="gather and download; no solving")
    parser.add_argument("--out", type=Path, default=Path("spike-out/corpus"))
    parser.add_argument("--budget", type=float, default=300.0, help="seconds to wait per image")
    args = parser.parse_args(argv)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    print("Searching Wikimedia Commons...")
    items = gather(session, args.per_query, args.limit)
    if not items:
        print("No usable images found.", file=sys.stderr)
        return 1

    nova = None
    if not args.fetch_only:
        api_key = os.environ.get("ASTROMETRY_API_KEY")
        if not api_key:
            print(
                "\nASTROMETRY_API_KEY is not set. Get a free key at\n"
                "  https://nova.astrometry.net/api_help\n"
                "or re-run with --fetch-only to build the corpus without solving.",
                file=sys.stderr,
            )
            return 2
        nova = Nova(api_key)

    print(f"\nProcessing {len(items)} image(s)...")
    images_dir = args.out / "images"
    for index, item in enumerate(items, start=1):
        label = item.title.removeprefix("File:")[:48]
        print(f"[{index}/{len(items)}] {label:<50} [{item.camera_class}] ", end="", flush=True)
        if not download(session, item, images_dir):
            print("download failed")
            continue
        if nova is None:
            print("fetched")
            continue
        solve(nova, item, args.budget)
        print(f"SOLVED {item.seconds}s" if item.solved else f"failed ({item.error or 'no match'})")

    args.out.mkdir(parents=True, exist_ok=True)
    summary = summarise(items)
    (args.out / "corpus-results.json").write_text(
        json.dumps({"summary": summary, "items": [asdict(i) for i in items]}, indent=2),
        encoding="utf-8",
    )
    write_attribution(items, args.out / "ATTRIBUTION.md")

    print("\n--- summary ---")
    print(f"  downscaled to: {summary['downscaled_to_px']}px wide before solving")
    for name, stats in summary["by_camera_class"].items():
        rate = stats["solve_rate_upper_bound"]
        shown = "n/a" if rate is None else f"{rate:.0%}"
        print(f"  {name:>12}: {stats['solved']}/{stats['attempted']} solved  (upper bound {shown})")
    print(f"\n  with timestamp: {summary['with_timestamp']}/{len(items)}")
    print(f"  with GPS:       {summary['with_gps']}/{len(items)}")
    print(f"  with exposure:  {summary['with_exposure']}/{len(items)}")
    print(
        f"  names a satellite (matcher ground-truth candidates): "
        f"{summary['ground_truth_candidates']}"
    )
    print(f"\n  {summary['caveat']}")
    print(f"\nWritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
