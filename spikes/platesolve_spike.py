#!/usr/bin/env python3
"""Milestone 0: can ordinary night-sky photographs be plate-solved?

Everything in satstreak downstream of pointing assumes the answer is yes. This
script answers it with evidence instead of hope, by running real photographs
through the nova.astrometry.net solver and recording, per image, whether it
solved, how long it took, and what pointing came back.

It is a spike, not library code: it is allowed to be scrappy, it is not imported
by the package, and it is expected to be deleted once milestone 6 replaces it
with a proper backend.

Usage
-----
Audit EXIF only, uploading nothing::

    python spikes/platesolve_spike.py photos/ --exif-only

Solve for real (needs a free API key from https://nova.astrometry.net/api_help)::

    export ASTROMETRY_API_KEY=xxxxxxxx
    python spikes/platesolve_spike.py photos/ --out spike-out/

Privacy
-------
Solving uploads your photographs to a third-party service. By default this script
strips all metadata before uploading and marks each submission non-public, but
the image itself still leaves your machine. Run ``--exif-only`` first if you want
to see what your camera records before sending anything anywhere.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import requests
    from PIL import Image
except ImportError:  # pragma: no cover - spike-only dependency
    sys.exit("Missing dependencies. Run: python -m pip install -e .[spike]")

API = "https://nova.astrometry.net/api"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

# 35 mm full-frame sensor width, used to turn the EXIF 35 mm-equivalent focal
# length into a field-of-view estimate. Handing the solver a scale range instead
# of letting it search blind is the difference between seconds and minutes on
# the wide fields phone cameras produce.
FULL_FRAME_WIDTH_MM = 36.0


@dataclass
class ExifFacts:
    """What the camera actually recorded. Missing values are the interesting ones."""

    width: int | None = None
    height: int | None = None
    timestamp_utc: str | None = None
    timestamp_is_guessed_tz: bool = False
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    altitude_m: float | None = None
    exposure_s: float | None = None
    iso: int | None = None
    focal_length_35mm: float | None = None
    estimated_fov_width_deg: float | None = None
    camera: str | None = None
    missing: list[str] = field(default_factory=list)


@dataclass
class SolveOutcome:
    image: str
    exif: ExifFacts
    solved: bool | None = None  # None when solving was not attempted
    seconds: float | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    field_radius_deg: float | None = None
    pixel_scale_arcsec: float | None = None
    orientation_deg: float | None = None
    job_url: str | None = None
    error: str | None = None


def _rational(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dms_to_degrees(dms: Any, ref: Any) -> float | None:
    try:
        degrees, minutes, seconds = (float(part) for part in dms)
    except (TypeError, ValueError):
        return None
    result = degrees + minutes / 60.0 + seconds / 3600.0
    if str(ref).upper().startswith(("S", "W")):
        result = -result
    return result


def read_exif(path: Path) -> ExifFacts:
    """Pull out the fields satstreak needs, and record which ones are absent."""
    facts = ExifFacts()
    with Image.open(path) as img:
        facts.width, facts.height = img.size
        exif = img.getexif()
        base = dict(exif)
        ifd = dict(exif.get_ifd(0x8769))  # ExifIFD
        gps = dict(exif.get_ifd(0x8825))  # GPSIFD

    make, model = base.get(0x010F), base.get(0x0110)
    if make or model:
        facts.camera = " ".join(str(part).strip() for part in (make, model) if part)

    raw_time = ifd.get(0x9003) or base.get(0x0132)  # DateTimeOriginal, DateTime
    if raw_time:
        try:
            naive = datetime.strptime(str(raw_time), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            naive = None
        if naive is not None:
            offset_text = ifd.get(0x9011)  # OffsetTimeOriginal
            tz = None
            if offset_text:
                try:
                    sign = -1 if str(offset_text)[0] == "-" else 1
                    hours, minutes = (int(x) for x in str(offset_text)[1:].split(":"))
                    tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
                except (ValueError, IndexError):
                    tz = None
            if tz is None:
                # No recorded offset. Assuming UTC is wrong but explicit; the flag
                # marks it so the number is never mistaken for a known time.
                tz = timezone.utc
                facts.timestamp_is_guessed_tz = True
            facts.timestamp_utc = naive.replace(tzinfo=tz).astimezone(timezone.utc).isoformat()

    facts.latitude_deg = _dms_to_degrees(gps.get(2), gps.get(1))
    facts.longitude_deg = _dms_to_degrees(gps.get(4), gps.get(3))
    altitude = _rational(gps.get(6))
    if altitude is not None:
        facts.altitude_m = -altitude if gps.get(5) in (1, b"\x01") else altitude

    facts.exposure_s = _rational(ifd.get(0x829A))  # ExposureTime
    iso = ifd.get(0x8827)
    facts.iso = int(iso) if isinstance(iso, int) else None
    facts.focal_length_35mm = _rational(ifd.get(0xA405))  # FocalLengthIn35mmFilm

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
    if facts.timestamp_is_guessed_tz:
        facts.missing.append("timezone_offset")
    return facts


def stripped_copy(path: Path) -> bytes:
    """Re-encode the image with no metadata, so no GPS or clock data is uploaded."""
    with Image.open(path) as img:
        use_png = img.mode in ("RGBA", "P", "LA")
        clean = Image.new(img.mode, img.size)
        clean.putdata(list(img.getdata()))
        buffer = io.BytesIO()
        if use_png:
            clean.save(buffer, format="PNG")
        else:
            clean.convert("RGB").save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


class Nova:
    """Minimal nova.astrometry.net client. Only the calls this spike needs."""

    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self.http = requests.Session()
        payload = self._post("/login", {"apikey": api_key})
        if payload.get("status") != "success":
            raise RuntimeError(f"astrometry.net login failed: {payload}")
        self.session_key = payload["session"]

    def _post(self, route: str, request: dict[str, Any], files: Any = None) -> dict[str, Any]:
        data = {"request-json": json.dumps(request)}
        response = self.http.post(API + route, data=data, files=files, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def _get(self, route: str) -> dict[str, Any]:
        response = self.http.get(API + route, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def submit(self, name: str, blob: bytes, fov_width_deg: float | None) -> int:
        request: dict[str, Any] = {
            "session": self.session_key,
            # Submissions default to public on nova. These are the user's own
            # night photographs, so they are kept private and unmodifiable.
            "publicly_visible": "n",
            "allow_modifications": "d",
            "allow_commercial_use": "d",
        }
        if fov_width_deg:
            request.update(
                scale_units="degwidth",
                scale_type="ul",
                scale_lower=round(fov_width_deg * 0.7, 3),
                scale_upper=round(fov_width_deg * 1.3, 3),
            )
        payload = self._post("/upload", request, files={"file": (name, blob)})
        if payload.get("status") != "success":
            raise RuntimeError(f"upload rejected: {payload}")
        return int(payload["subid"])

    def job_for(self, subid: int) -> int | None:
        """The job id once the submission has been picked up, otherwise None."""
        jobs = [j for j in self._get(f"/submissions/{subid}").get("jobs", []) if j]
        return int(jobs[0]) if jobs else None

    def job_status(self, job_id: int) -> str:
        """One of ``solving``, ``success`` or ``failure``."""
        return str(self._get(f"/jobs/{job_id}").get("status") or "unknown")

    def wait(self, subid: int, budget_s: float, poll_s: float = 5.0) -> tuple[bool, int | None]:
        """Block until one submission resolves. Returns (solved, job_id).

        Convenient for a handful of images. For a whole corpus, submit
        everything first and poll the set: nova solves asynchronously, so
        waiting on each image in turn serialises work the server would
        otherwise do in parallel.
        """
        deadline = time.monotonic() + budget_s
        job_id: int | None = None
        while time.monotonic() < deadline:
            if job_id is None:
                job_id = self.job_for(subid)
            if job_id is not None:
                status = self.job_status(job_id)
                if status == "success":
                    return True, job_id
                if status == "failure":
                    return False, job_id
            time.sleep(poll_s)
        raise TimeoutError(f"no verdict within {budget_s:.0f}s")

    def calibration(self, job_id: int) -> dict[str, Any]:
        return self._get(f"/jobs/{job_id}/calibration/")


def process(path: Path, nova: Nova | None, budget_s: float, strip: bool) -> SolveOutcome:
    outcome = SolveOutcome(image=path.name, exif=read_exif(path))
    if nova is None:
        return outcome

    started = time.monotonic()
    try:
        blob = stripped_copy(path) if strip else path.read_bytes()
        subid = nova.submit(path.name, blob, outcome.exif.estimated_fov_width_deg)
        solved, job_id = nova.wait(subid, budget_s)
        outcome.solved = solved
        outcome.seconds = round(time.monotonic() - started, 1)
        if job_id is not None:
            outcome.job_url = f"https://nova.astrometry.net/status/{job_id}"
        if solved and job_id is not None:
            cal = nova.calibration(job_id)
            outcome.ra_deg = cal.get("ra")
            outcome.dec_deg = cal.get("dec")
            outcome.field_radius_deg = cal.get("radius")
            outcome.pixel_scale_arcsec = cal.get("pixscale")
            outcome.orientation_deg = cal.get("orientation")
    except Exception as exc:  # noqa: BLE001 - a spike records failures, it does not raise
        outcome.solved = False
        outcome.seconds = round(time.monotonic() - started, 1)
        outcome.error = f"{type(exc).__name__}: {exc}"
    return outcome


def summarise(outcomes: list[SolveOutcome]) -> dict[str, Any]:
    attempted = [o for o in outcomes if o.solved is not None]
    solved = [o for o in attempted if o.solved]
    times = sorted(o.seconds for o in solved if o.seconds is not None)
    return {
        "images": len(outcomes),
        "attempted": len(attempted),
        "solved": len(solved),
        "solve_rate": round(len(solved) / len(attempted), 3) if attempted else None,
        "median_solve_seconds": times[len(times) // 2] if times else None,
        "with_gps": sum(1 for o in outcomes if o.exif.latitude_deg is not None),
        "with_timestamp": sum(1 for o in outcomes if o.exif.timestamp_utc is not None),
        "with_timezone_offset": sum(
            1 for o in outcomes if o.exif.timestamp_utc and not o.exif.timestamp_is_guessed_tz
        ),
        "with_focal_length": sum(1 for o in outcomes if o.exif.focal_length_35mm is not None),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plate-solve feasibility spike.")
    parser.add_argument("target", type=Path, help="an image, or a directory of images")
    parser.add_argument("--exif-only", action="store_true", help="upload nothing; audit EXIF only")
    parser.add_argument(
        "--out", type=Path, default=Path("spike-out"), help="where to write results"
    )
    parser.add_argument("--budget", type=float, default=300.0, help="seconds to wait per image")
    parser.add_argument(
        "--keep-metadata",
        action="store_true",
        help="upload the original file including GPS and timestamps (not recommended)",
    )
    args = parser.parse_args(argv)

    if args.target.is_dir():
        paths = sorted(p for p in args.target.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    else:
        paths = [args.target]
    if not paths:
        print(f"No images found in {args.target}", file=sys.stderr)
        return 2

    nova = None
    if not args.exif_only:
        api_key = os.environ.get("ASTROMETRY_API_KEY")
        if not api_key:
            print(
                "ASTROMETRY_API_KEY is not set. Get a free key at\n"
                "  https://nova.astrometry.net/api_help\n"
                "then set it, or re-run with --exif-only to audit metadata without uploading.",
                file=sys.stderr,
            )
            return 2
        print(f"Uploading {len(paths)} image(s) to nova.astrometry.net.")
        print(
            "WARNING: metadata will be kept."
            if args.keep_metadata
            else "Metadata is stripped before upload."
        )
        nova = Nova(api_key)

    outcomes: list[SolveOutcome] = []
    for index, path in enumerate(paths, start=1):
        print(f"[{index}/{len(paths)}] {path.name} ... ", end="", flush=True)
        outcome = process(path, nova, args.budget, strip=not args.keep_metadata)
        outcomes.append(outcome)
        if outcome.solved is None:
            print(f"exif ok (missing: {', '.join(outcome.exif.missing) or 'nothing'})")
        elif outcome.solved:
            print(
                f"SOLVED in {outcome.seconds}s  "
                f"ra={outcome.ra_deg:.3f} dec={outcome.dec_deg:.3f} "
                f"radius={outcome.field_radius_deg:.2f}deg"
            )
        else:
            print(f"FAILED ({outcome.error or 'solver found no match'})")

    summary = summarise(outcomes)
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"summary": summary, "results": [asdict(o) for o in outcomes]}
    destination = args.out / "platesolve-results.json"
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n--- summary ---")
    for key, value in summary.items():
        print(f"{key:>22}: {value}")
    print(f"\nWritten to {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
