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
from satstreak.solve import PlateSolver
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
#: the best coarse value. The search covers a **full** turn: a trail's own angle
#: wraps at 180 because a trail is an axis rather than an arrow, but the sensor's
#: orientation does not. Rolling by 180 degrees sends every pixel to the opposite
#: side of the frame, so roll 30 and roll 210 place the same track in different
#: places. Searching only half a turn leaves half the answers unreachable.
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

    coarse = [(total_for(r), r) for r in _frange(0.0, 360.0, ROLL_COARSE_STEP_DEG)]
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
    return best_roll % 360.0, best_total


def _frange(start: float, stop: float, step: float) -> list[float]:
    count = max(1, round((stop - start) / step))
    return [start + i * step for i in range(count)]


def scan_image(
    image: np.ndarray,
    observation: Observation,
    pointing: Pointing | None = None,
    *,
    solver: PlateSolver | None = None,
    fov_hint_deg: float | None = None,
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
        pointing: Where the camera was aimed. Either this or `solver` must be
            given: without one there is no way to relate pixels to sky, and
            guessing would produce confident nonsense.
        solver: Recovers the pointing from the stars in the frame, removing the
            last thing the user has to supply. When it succeeds its answer is
            used; when it fails and no `pointing` was given, the scan stops with
            `NO_POINTING` rather than proceeding on a guess.
        search_roll: Solve for the sensor rotation rather than trusting the one
            given. Roll is the single pointing parameter a photographer cannot
            report, so this is on by default in the CLI.

    Returns:
        `ImageStatus.NO_STREAKS` when the frame was swept and held nothing, or
        `FOUND` with one finding per detected trail. Each finding separately
        reports whether it was matched, was ambiguous, or matched nothing.
    """
    settings = detector or DetectorSettings()
    diagnostics: dict = {"detector": settings.to_dict(), "observation": observation.to_dict()}

    if solver is not None:
        solved = solver.solve(image, observation, fov_hint_deg=fov_hint_deg)
        diagnostics["plate_solve"] = {
            "attempted": True,
            "solved": solved.solved,
            "seconds": solved.seconds,
            "stars_used": solved.stars_used,
            "centre_ra_deg": solved.centre_ra_deg,
            "centre_dec_deg": solved.centre_dec_deg,
            "message": solved.message,
        }
        if solved.pointing is not None:
            pointing = solved.pointing
            # The solver reports orientation against north while roll here is
            # measured against the zenith; the search below recovers the
            # difference rather than it being derived.
            search_roll = True
        elif pointing is None:
            return IdentifyResult(
                status=ImageStatus.NO_POINTING,
                message=("Could not work out where the camera was aimed. " + solved.message),
                diagnostics=diagnostics,
            )

    if pointing is None:
        raise ValueError("scan_image needs either a pointing or a solver")

    streaks = detect_streaks(image, detector)

    diagnostics |= {
        "pointing": {
            "altitude_deg": pointing.altitude_deg,
            "azimuth_deg": pointing.azimuth_deg,
            "roll_deg": pointing.roll_deg,
            "field_width_deg": round(pointing.field_width_deg, 2),
            "is_wide_field": pointing.is_wide_field,
        },
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
    pointing: Pointing | None = None,
    **kwargs,
) -> IdentifyResult:
    """`scan_image` for a photograph on disk."""
    return scan_image(load_greyscale(path), observation, pointing, **kwargs)
