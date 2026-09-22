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

from pathlib import Path

import numpy as np

from satstreak.detect import DetectorSettings, detect_streaks
from satstreak.geometry import Pointing
from satstreak.match import match_streak
from satstreak.predict import Prediction, predict
from satstreak.propagate import Propagator
from satstreak.types import IdentifyResult, ImageStatus, Observation


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
) -> IdentifyResult:
    """Find every trail in a frame and identify what made it.

    Args:
        image: Greyscale, 2-D. Use `load_greyscale` for a file on disk.
        pointing: Where the camera was aimed. Required: without it there is no
            way to relate pixels to sky, and guessing would produce confident
            nonsense.

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

    predictions: list[Prediction] = predict(
        observation,
        pointing,
        propagator=propagator,
        group=group,
        min_altitude_deg=min_altitude_deg,
        samples=samples,
    )
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
