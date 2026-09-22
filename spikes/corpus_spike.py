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
from collections import Counter
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

#: Consecutive transport errors tolerated per item before giving up on it.
MAX_POLL_ERRORS = 4

#: Pause between individual API requests. Nova is a free service run for
#: everyone; a batch poll without this is several requests per second.
REQUEST_PACING_S = 0.5


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
    """Wall seconds from the start of the batch to this job's verdict, not the
    solver's own runtime. Jobs are submitted together and queued, so this is a
    throughput figure, not a per-image cost."""
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


def submit_all(nova: Nova, items: list[CorpusItem], pause_s: float = 1.0) -> dict[int, int]:
    """Submit every downloaded image up front. Returns {item index: submission id}."""
    pending: dict[int, int] = {}
    for index, item in enumerate(items):
        if not item.local_path:
            continue
        try:
            blob = Path(item.local_path).read_bytes()
            pending[index] = nova.submit(
                Path(item.local_path).name, blob, item.exif.estimated_fov_width_deg
            )
        except Exception as exc:  # noqa: BLE001 - a spike records failures, it does not raise
            item.solved = None  # never reached the solver, so nothing was learned
            item.error = f"submit failed: {type(exc).__name__}: {exc}"
        time.sleep(pause_s)  # do not hammer the queue
    return pending


def collect_all(
    nova: Nova,
    items: list[CorpusItem],
    pending: dict[int, int],
    budget_s: float,
    poll_s: float = 5.0,
) -> None:
    """Poll the whole submitted set until every job resolves or the budget runs out.

    Two rules matter more than speed here.

    A transport error is never recorded as ``solved = False``. Not being able to
    ask is not the same as being told no, and conflating them would deflate the
    solve rate with network noise while looking like a real measurement. Items
    that never yield a verdict keep ``solved = None`` and are excluded from the
    denominator.

    Requests are paced. Polling 45 submissions in a tight loop is several
    requests per second against a free public service, which is both rude and
    self-defeating: nova throttles it, and the throttling then looks like
    failures to solve.
    """
    started = time.monotonic()
    deadline = started + budget_s
    jobs: dict[int, int] = {}
    consecutive_errors: dict[int, int] = {}
    total = len(pending)

    def retire(index: int, mark: str, note: str) -> None:
        pending.pop(index, None)
        label = items[index].title.removeprefix("File:")[:44]
        done = total - len(pending)
        print(f"  [{done}/{total}] {mark:<10} {label:<46} {note}")

    while pending and time.monotonic() < deadline:
        for index in list(pending):
            if time.monotonic() >= deadline:
                break
            item = items[index]
            try:
                if index not in jobs:
                    job_id = nova.job_for(pending[index])
                    if job_id is None:
                        continue  # not picked up off the queue yet
                    jobs[index] = job_id
                status = nova.job_status(jobs[index])
                consecutive_errors[index] = 0
            except Exception as exc:  # noqa: BLE001 - a spike records, it does not raise
                consecutive_errors[index] = consecutive_errors.get(index, 0) + 1
                if consecutive_errors[index] >= MAX_POLL_ERRORS:
                    item.solved = None  # unknown, NOT a failure to solve
                    item.error = (
                        f"gave up after {MAX_POLL_ERRORS} consecutive polling errors; "
                        f"last was {type(exc).__name__}: {exc}"
                    )
                    retire(index, "no verdict", "(polling kept failing)")
                continue
            finally:
                # Paces every request, which also paces the whole cycle.
                time.sleep(REQUEST_PACING_S)

            if status not in ("success", "failure"):
                continue

            item.solved = status == "success"
            item.seconds = round(time.monotonic() - started, 1)
            item.job_url = f"https://nova.astrometry.net/status/{jobs[index]}"
            if item.solved:
                try:
                    cal = nova.calibration(jobs[index])
                    item.ra_deg = cal.get("ra")
                    item.dec_deg = cal.get("dec")
                    item.field_radius_deg = cal.get("radius")
                except Exception as exc:  # noqa: BLE001 - solved is still solved
                    item.error = f"calibration unavailable: {type(exc).__name__}: {exc}"
            retire(index, "SOLVED" if item.solved else "failed", f"[{item.camera_class}]")

        if pending:
            time.sleep(poll_s)

    # Ran out of time rather than out of answers: also unknown, not a failure.
    for index in list(pending):
        items[index].solved = None
        items[index].error = f"no verdict within the {budget_s:.0f}s batch budget"
        retire(index, "no verdict", "(batch budget expired)")


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
            # Items nova never gave a verdict on. Excluded from the rate above
            # rather than silently counted as failures, and surfaced here so a
            # rate computed over a handful of images cannot look authoritative.
            "no_verdict": len(subset) - len(attempted),
            "median_seconds_to_verdict": times[len(times) // 2] if times else None,
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
        # The raw model strings, so a thin phone bucket can be told apart from a
        # classifier that simply does not recognise the phones that are present.
        "camera_models": dict(
            sorted(
                Counter(i.exif.camera or "(none recorded)" for i in items).items(),
                key=lambda kv: (-kv[1], kv[0]),
            )
        ),
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
    parser.add_argument(
        "--budget",
        type=float,
        default=900.0,
        help="seconds to wait for the whole batch (nova solves the queue in parallel)",
    )
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

    print()
    print(f"Downloading {len(items)} image(s)...")
    images_dir = args.out / "images"
    for index, item in enumerate(items, start=1):
        label = item.title.removeprefix("File:")[:48]
        ok = download(session, item, images_dir)
        mark = "ok    " if ok else "FAILED"
        print(f"  [{index}/{len(items)}] {mark} {label:<50} [{item.camera_class}]")

    if nova is not None:
        print()
        print("Submitting to nova.astrometry.net...")
        pending = submit_all(nova, items)
        print(f"  {len(pending)} submitted; polling (batch budget {args.budget:.0f}s)")
        collect_all(nova, items, pending, args.budget)

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
        print(
            f"  {name:>12}: {stats['solved']}/{stats['attempted']} solved  "
            f"(upper bound {shown}, {stats['no_verdict']} without a verdict)"
        )
    print(f"\n  with timestamp: {summary['with_timestamp']}/{len(items)}")
    print(f"  with GPS:       {summary['with_gps']}/{len(items)}")
    print(f"  with exposure:  {summary['with_exposure']}/{len(items)}")
    print(
        f"  names a satellite (matcher ground-truth candidates): "
        f"{summary['ground_truth_candidates']}"
    )
    print()
    print("  camera models seen:")
    for model, count in list(summary["camera_models"].items())[:12]:
        print(f"    {count:>3}x  {model}")
    print(f"\n  {summary['caveat']}")
    print(f"\nWritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
