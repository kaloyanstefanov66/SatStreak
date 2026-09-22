"""The whole pipeline: a photograph in, named satellites out.

Ties the stages together. Detection sweeps the frame, prediction supplies the
candidates, and the matcher decides. The pointing is a parameter rather than
something discovered here, which is what lets this work today: supplied by hand,
recovered by a plate solve when that exists, or synthesised for a test, the rest
of the pipeline does not care where it came from.

Every outcome the caller can receive is a real one. An unsolvable frame, an empty
sky, a trail that matches nothing and a confident identification are four
different answers and are reported as four different answers.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

from satstreak.detect import DetectorSettings, detect_streaks
from satstreak.geometry import Pointing
from satstreak.match import match_streak
from satstreak.predict import Prediction, predict
from satstreak.propagate import Propagator
from satstreak.types import IdentifyResult, ImageStatus, Observation, Streak


def load_greyscale(path: str | Path) -> np.ndarray:
    """Read an image as a 2-D float array.

    Kept separate so the pipeline itself never touches image formats, and so a
    caller working from an array -- a test, or a web upload already decoded --
    can skip it entirely.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - optional extra
        raise RuntimeError(
            "Reading image files needs Pillow. Install it with: pip install satstreak[images]"
        ) from exc

    Image.MAX_IMAGE_PIXELS = None  # night panoramas exceed the default guard
    with Image.open(path) as img:
        return np.asarray(img.convert("L"), dtype=np.float32)


#: Coarse step for the roll search, in degrees, followed by a fine pass around
#: the best coarse value. A trail's orientation wraps at 180, so the search only
#: needs half a turn.
ROLL_COARSE_STEP_DEG = 3.0
ROLL_FINE_STEP_DEG = 0.25


def solve_roll(
    streaks: list[Streak],
    overhead: list[Prediction],
    pointing: Pointing,
) -> tuple[float, float]:
    """Find the sensor rotation that best explains the detected trails.

    Roll is the one pointing parameter a photographer cannot report. Altitude,
    azimuth and field of view can all be estimated from where they aimed and
    what lens they used; how the camera was rotated about its optical axis is
    not something anyone knows. A plate solve would supply it, and until one
    exists the alternative to searching for it is requiring a number the user
    cannot give.

    Searching is cheap because roll does not affect propagation, only
    projection: the satellites are propagated once and re-projected per
    candidate angle.

    Returns:
        The best roll in degrees, and the total score it achieved, so a caller
        can tell a confident fit from a search that found nothing anywhere.
    """
    from satstreak.match import score as score_candidate

    def total_for(roll: float) -> float:
        aimed = replace(pointing, roll_deg=roll)
        projected = [
            Prediction(track=p.track, pixels=aimed.project_track(p.track)) for p in overhead
        ]
        running = 0.0
        for streak in streaks:
            best = 0.0
            for prediction in projected:
                candidate = score_candidate(streak, prediction, aimed)
                if candidate is not None and candidate.score > best:
                    best = candidate.score
            running += best
        return running

    coarse = [(total_for(r), r) for r in _frange(0.0, 180.0, ROLL_COARSE_STEP_DEG)]
    best_total, best_roll = max(coarse)

    fine = [
        (total_for(r), r)
        for r in _frange(
            best_roll - ROLL_COARSE_STEP_DEG, best_roll + ROLL_COARSE_STEP_DEG, ROLL_FINE_STEP_DEG
        )
    ]
    fine_total, fine_roll = max(fine)
    if fine_total >= best_total:
        best_total, best_roll = fine_total, fine_roll
    return best_roll % 180.0, best_total


def _frange(start: float, stop: float, step: float) -> list[float]:
    count = max(1, round((stop - start) / step))
    return [start + i * step for i in range(count)]


def scan_image(
    image: np.ndarray,
    observation: Observation,
    pointing: Pointing,
    *,
    propagator: Propagator | None = None,
    group: str = "active",
    min_altitude_deg: float = 10.0,
    detector: DetectorSettings | None = None,
    samples: int = 9,
    search_roll: bool = False,
) -> IdentifyResult:
    """Find every trail in a frame and identify what made it.

    Args:
        image: Greyscale, 2-D. Use `load_greyscale` for a file on disk.
        pointing: Where the camera was aimed. Required: without it there is no
            way to relate pixels to sky, and guessing would produce confident
            nonsense.
        search_roll: Solve for the sensor rotation rather than trusting the one
            given. Roll is the single pointing parameter a photographer cannot
            report, so this is on by default in the CLI.

    Returns:
        `ImageStatus.NO_STREAKS` when the frame was swept and held nothing, or
        `FOUND` with one finding per detected trail. Each finding separately
        reports whether it was matched, was ambiguous, or matched nothing.
    """
    streaks = detect_streaks(image, detector)
    settings = detector or DetectorSettings()

    diagnostics = {
        "pointing": {
            "altitude_deg": pointing.altitude_deg,
            "azimuth_deg": pointing.azimuth_deg,
            "roll_deg": pointing.roll_deg,
            "field_width_deg": round(pointing.field_width_deg, 2),
            "is_wide_field": pointing.is_wide_field,
        },
        "observation": observation.to_dict(),
        "detector": settings.to_dict(),
        "streaks_detected": len(streaks),
    }

    if not streaks:
        return IdentifyResult(
            status=ImageStatus.NO_STREAKS,
            message="No satellite trails found in this photograph.",
            diagnostics=diagnostics,
        )

    # Everything above the horizon, before any frame test, so that a roll search
    # can re-project the same tracks without propagating again.
    overhead: list[Prediction] = predict(
        observation,
        None,
        propagator=propagator,
        group=group,
        min_altitude_deg=min_altitude_deg,
        samples=samples,
    )

    if search_roll:
        best_roll, total = solve_roll(streaks, overhead, pointing)
        diagnostics["roll_searched"] = True
        diagnostics["roll_deg"] = round(best_roll, 2)
        diagnostics["roll_search_total_score"] = round(total, 3)
        pointing = replace(pointing, roll_deg=best_roll)
        diagnostics["pointing"]["roll_deg"] = round(best_roll, 2)

    predictions = [
        Prediction(track=p.track, pixels=pointing.project_track(p.track)) for p in overhead
    ]
    predictions = [p for p in predictions if p.pixels is not None and p.pixels.crosses_frame]
    diagnostics["candidates_in_frame"] = len(predictions)

    findings = tuple(match_streak(s, predictions, pointing) for s in streaks)
    return IdentifyResult(
        status=ImageStatus.FOUND,
        findings=findings,
        message=f"{len(findings)} trail(s) found.",
        diagnostics=diagnostics,
    )


def scan_file(
    path: str | Path,
    observation: Observation,
    pointing: Pointing,
    **kwargs,
) -> IdentifyResult:
    """`scan_image` for a photograph on disk."""
    return scan_image(load_greyscale(path), observation, pointing, **kwargs)
