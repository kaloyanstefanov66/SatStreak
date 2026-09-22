#!/usr/bin/env python3
"""Milestone 0, part three: plate-solve locally instead of through nova.

nova.astrometry.net's public queue does not process batches — measured on
2026-09-22, a single image went unassigned for over five minutes and thirty
produced no verdicts in twenty-five. Milestone 6 has to solve a corpus
repeatedly, so the solver has to run locally. This spike establishes whether the
`astrometry` package can do it, and how well it copes with wide fields.

Run it under Linux or WSL::

    python -m pip install astrometry scipy pillow
    python spikes/localsolve_spike.py spike-out/corpus/images/ --cache ~/astrometry-cache

Why there is an extractor in here
---------------------------------
The `astrometry` package calls the real Astrometry.net library but takes a list
of star pixel positions rather than an image; it deliberately leaves out image
handling. So extraction is ours to write.

That is a feature rather than a cost. Extraction quality is one of the things
that decides whether a phone photograph solves at all, and having it as our own
testable stage means it can be measured and improved instead of being a black box
inside someone else's binary. The implementation below is deliberately simple —
the point of the spike is to find out whether simple is good enough.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import astrometry
    import numpy as np
    from PIL import Image
    from scipy import ndimage
except ImportError as exc:  # pragma: no cover - spike-only dependency
    sys.exit(
        f"Missing dependency ({exc}).\n"
        "This spike needs Linux or WSL. Run:\n"
        "  python -m pip install astrometry scipy pillow"
    )

Image.MAX_IMAGE_PIXELS = None  # Commons panoramas exceed Pillow's decompression guard

#: Tycho-2 indexes, "good for images wider than 1 degree". 0.36 GB for every
#: scale, against 30+ GB for the narrow-field Gaia series, which a 70-degree
#: phone frame has no use for.
SERIES = astrometry.series_4100


@dataclass
class SolveOutcome:
    image: str
    width: int
    height: int
    stars_found: int
    solved: bool
    seconds: float
    ra_deg: float | None = None
    dec_deg: float | None = None
    scale_arcsec_per_px: float | None = None
    field_width_deg: float | None = None
    error: str | None = None


def extract_stars(
    path: Path, max_stars: int = 500, threshold_sigma: float = 5.0, downscale_to: int = 2000
) -> tuple[list[tuple[float, float]], int, int]:
    """Find star-like point sources and return their pixel centroids, brightest first.

    Crude on purpose: greyscale, subtract a coarse background, threshold at a few
    sigma above the residual noise, label connected components, and take centroids.
    Nothing here is clever, which is the point — if this is enough to solve a phone
    photograph, the pipeline does not need anything cleverer.
    """
    with Image.open(path) as img:
        full_width, full_height = img.size
        grey = img.convert("L")
        if downscale_to and max(grey.size) > downscale_to:
            ratio = downscale_to / max(grey.size)
            grey = grey.resize(
                (max(1, int(grey.width * ratio)), max(1, int(grey.height * ratio))),
                Image.LANCZOS,
            )
        data = np.asarray(grey, dtype=np.float32)

    scale_back = full_width / data.shape[1]

    # A wide night photograph has strong large-scale gradients: light pollution,
    # the Milky Way, vignetting. Thresholding without removing them finds the
    # bright corner of the sky rather than the stars in it.
    background = ndimage.uniform_filter(data, size=max(8, min(data.shape) // 20))
    residual = data - background

    noise = float(np.median(np.abs(residual - np.median(residual)))) * 1.4826
    if noise <= 0:
        noise = float(residual.std()) or 1.0
    mask = residual > threshold_sigma * noise

    labels, count = ndimage.label(mask)
    if count == 0:
        return [], full_width, full_height

    sizes = ndimage.sum(mask, labels, range(1, count + 1))
    peaks = ndimage.maximum(residual, labels, range(1, count + 1))
    centroids = ndimage.center_of_mass(residual, labels, range(1, count + 1))

    stars = [
        # Reject single hot pixels and large blobs (aeroplanes, clouds, the moon,
        # foreground). Stars occupy a few pixels.
        (float(peak), (float(cy) * scale_back, float(cx) * scale_back))
        for size, peak, (cy, cx) in zip(sizes, peaks, centroids, strict=True)
        if 2 <= size <= 200
    ]
    stars.sort(key=lambda item: item[0], reverse=True)
    return [(x, y) for _, (y, x) in stars[:max_stars]], full_width, full_height


def solve_one(path: Path, solver: astrometry.Solver, hint_deg: float | None) -> SolveOutcome:
    started = time.monotonic()
    try:
        stars, width, height = extract_stars(path)
    except Exception as exc:  # noqa: BLE001 - a spike records failures
        return SolveOutcome(
            image=path.name,
            width=0,
            height=0,
            stars_found=0,
            solved=False,
            seconds=round(time.monotonic() - started, 1),
            error=f"extraction failed: {type(exc).__name__}: {exc}",
        )

    if len(stars) < 10:
        return SolveOutcome(
            image=path.name,
            width=width,
            height=height,
            stars_found=len(stars),
            solved=False,
            seconds=round(time.monotonic() - started, 1),
            error="too few stars extracted to attempt a solve",
        )

    size_hint = None
    if hint_deg:
        # Arcseconds per pixel, bracketed generously: a hint that is wrong in the
        # wrong direction is worse than no hint at all.
        centre = hint_deg * 3600.0 / width
        size_hint = astrometry.SizeHint(
            lower_arcsec_per_pixel=centre * 0.5, upper_arcsec_per_pixel=centre * 2.0
        )

    try:
        solution = solver.solve(
            stars=stars,
            size_hint=size_hint,
            position_hint=None,
            solution_parameters=astrometry.SolutionParameters(),
        )
    except Exception as exc:  # noqa: BLE001 - a spike records failures
        return SolveOutcome(
            image=path.name,
            width=width,
            height=height,
            stars_found=len(stars),
            solved=False,
            seconds=round(time.monotonic() - started, 1),
            error=f"solver raised: {type(exc).__name__}: {exc}",
        )

    elapsed = round(time.monotonic() - started, 1)
    if not solution.has_match():
        return SolveOutcome(
            image=path.name,
            width=width,
            height=height,
            stars_found=len(stars),
            solved=False,
            seconds=elapsed,
            error="no match",
        )

    match = solution.best_match()
    scale = match.scale_arcsec_per_pixel
    return SolveOutcome(
        image=path.name,
        width=width,
        height=height,
        stars_found=len(stars),
        solved=True,
        seconds=elapsed,
        ra_deg=float(match.center_ra_deg),
        dec_deg=float(match.center_dec_deg),
        scale_arcsec_per_px=float(scale),
        field_width_deg=round(float(scale) * width / 3600.0, 2),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local plate-solving spike.")
    parser.add_argument("target", type=Path, help="an image, or a directory of images")
    parser.add_argument("--cache", type=Path, required=True, help="index file cache directory")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many images")
    parser.add_argument(
        "--hint-deg",
        type=float,
        default=None,
        help="approximate field width in degrees, to narrow the scale search",
    )
    parser.add_argument("--out", type=Path, default=Path("spike-out/localsolve.json"))
    args = parser.parse_args(argv)

    suffixes = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
    if args.target.is_dir():
        paths = sorted(p for p in args.target.iterdir() if p.suffix.lower() in suffixes)
    else:
        paths = [args.target]
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print(f"No images found in {args.target}", file=sys.stderr)
        return 2

    print(f"Loading {SERIES.__class__.__name__ if False else 'series_4100'} indexes...", flush=True)
    t0 = time.monotonic()
    solver = astrometry.Solver(SERIES.index_files(cache_directory=str(args.cache), scales=None))
    print(f"  ready in {time.monotonic() - t0:.0f}s\n", flush=True)

    outcomes: list[SolveOutcome] = []
    for index, path in enumerate(paths, start=1):
        print(f"[{index}/{len(paths)}] {path.name[:46]:<48} ", end="", flush=True)
        outcome = solve_one(path, solver, args.hint_deg)
        outcomes.append(outcome)
        if outcome.solved:
            print(
                f"SOLVED {outcome.seconds:5.1f}s  {outcome.stars_found:4d} stars  "
                f"field {outcome.field_width_deg} deg"
            )
        else:
            print(
                f"failed {outcome.seconds:5.1f}s  {outcome.stars_found:4d} stars  ({outcome.error})"
            )

    solved = [o for o in outcomes if o.solved]
    times = sorted(o.seconds for o in solved)
    print("\n--- summary ---")
    print(f"  solved: {len(solved)}/{len(outcomes)}  ({len(solved) / len(outcomes):.0%})")
    if times:
        print(f"  median solve time: {times[len(times) // 2]:.1f}s")
        widths = sorted(o.field_width_deg or 0 for o in solved)
        print(f"  field widths: {widths[0]:.1f} to {widths[-1]:.1f} deg")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps([asdict(o) for o in outcomes], indent=2), encoding="utf-8")
    print(f"\nWritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
