"""Deciding which satellite, if any, made a given trail.

Scoring follows the project's central bet: **geometry first, timing only to rule
out.** A clock that is a few seconds wrong moves a satellite along its path
rather than off it, so the line a satellite draws is far more robust evidence
than the moment it occupied any point on that line. Direction of travel is
therefore weighted most heavily, then how closely the predicted path passes the
observed trail, then length.

Scores are **not yet calibrated**. They combine three residuals into a number in
[0, 1] that behaves sensibly — better fits score higher, and an implausible fit
scores near zero — but nothing yet guarantees that 0.9 means nine in ten such
calls are right. Calibration needs the evaluation set from milestone 6. Until
then the score is a ranking, and the thresholds below are deliberately
conservative so that the failure mode is silence rather than a confident
mistake.
"""

from __future__ import annotations

import math

from satstreak.geometry import Pointing
from satstreak.predict import Prediction
from satstreak.types import Candidate, Finding, FindingStatus, Streak

#: Angular tolerance on direction of travel. Generous enough to absorb detector
#: end-point error on a short trail, tight enough that a satellite crossing at a
#: different angle is excluded.
DIRECTION_SIGMA_DEG = 6.0

#: Tolerance on how far the predicted path passes from the observed trail.
#: Dominated in practice by pointing error from the plate solve.
OFFSET_SIGMA_DEG = 0.75

#: Tolerance on length, as a natural-log ratio. Wide on purpose: a detector
#: clips faint trail ends, and the exposure recorded in EXIF is not always the
#: exposure that happened.
LENGTH_LOG_SIGMA = 0.45

#: Below this, no candidate is good enough to name.
MIN_SCORE = 0.25

#: The leader must beat the runner-up by this factor, or the result is ambiguous
#: rather than a match.
DECISIVE_RATIO = 1.6


def _angular_difference(a: float, b: float) -> float:
    """Smallest angle between two orientations that wrap at 180 degrees."""
    diff = abs(a - b) % 180.0
    return min(diff, 180.0 - diff)


def _point_to_segment_px(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from a point to a line segment, in pixels."""
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if length_squared == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_squared))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def score(streak: Streak, prediction: Prediction, pointing: Pointing) -> Candidate | None:
    """Score one predicted track against one observed trail.

    Returns None when the prediction has no usable path in the frame, which is
    not a poor score but an absence of evidence either way.
    """
    pixels = prediction.pixels
    if pixels is None or not pixels.crosses_frame or pixels.angle_deg is None:
        return None

    predicted_angle = pixels.angle_deg
    direction_error = _angular_difference(streak.angle_deg, predicted_angle)

    seen = pixels.visible_points
    start, end = seen[0], seen[-1]
    midpoint_x = (streak.x1 + streak.x2) / 2.0
    midpoint_y = (streak.y1 + streak.y2) / 2.0
    offset_px = _point_to_segment_px(midpoint_x, midpoint_y, start.x, start.y, end.x, end.y)
    offset_deg = offset_px * pointing.scale_arcsec_per_px / 3600.0

    predicted_length = pixels.length_px
    if predicted_length <= 0.0:
        return None
    length_ratio = streak.length_px / predicted_length

    direction_term = math.exp(-((direction_error / DIRECTION_SIGMA_DEG) ** 2))
    offset_term = math.exp(-((offset_deg / OFFSET_SIGMA_DEG) ** 2))
    length_term = math.exp(-((math.log(length_ratio) / LENGTH_LOG_SIGMA) ** 2))

    # A product rather than a weighted sum: every term must be plausible. A
    # satellite crossing at the right place but the wrong angle is not a
    # three-quarters match, it is the wrong satellite.
    combined = direction_term * offset_term * length_term

    notes: list[str] = []
    if prediction.track.elements_are_stale:
        notes.append(
            f"orbital elements were {prediction.track.element_age.days} days old, "
            "so the predicted path is coarser than usual"
        )
    if pointing.is_wide_field:
        notes.append(
            f"{pointing.field_width_deg:.0f} degree field; lens distortion is not "
            "modelled, so positions away from centre are approximate"
        )

    return Candidate(
        norad_id=prediction.norad_id,
        name=prediction.name,
        score=min(1.0, max(0.0, combined)),
        angular_separation_deg=round(offset_deg, 4),
        direction_error_deg=round(direction_error, 3),
        length_ratio=round(length_ratio, 3),
        notes=tuple(notes),
    )


def match_streak(
    streak: Streak,
    predictions: list[Prediction],
    pointing: Pointing,
    *,
    min_score: float = MIN_SCORE,
    decisive_ratio: float = DECISIVE_RATIO,
) -> Finding:
    """Decide what, if anything, made this trail.

    The three outcomes are deliberate. A single clear leader is a match. Several
    comparable fits are ambiguous, not a match with runners-up, because the tool
    volunteers findings and a reader cannot check them. Nothing plausible is
    `UNIDENTIFIED`, carrying no candidates at all so that a weak fit cannot be
    mistaken for a hedged answer.
    """
    scored = [c for c in (score(streak, p, pointing) for p in predictions) if c is not None]
    scored.sort(key=lambda c: c.score, reverse=True)

    plausible = tuple(c for c in scored if c.score >= min_score)
    if not plausible:
        best = f" Closest was {scored[0].name} at {scored[0].score:.2f}." if scored else ""
        return Finding(
            streak=streak,
            status=FindingStatus.UNIDENTIFIED,
            message=(
                f"No catalogued satellite fits this trail (checked {len(scored)})."
                f"{best} It may be an aircraft, a meteor, a sensor artefact, or an "
                "object not in the catalogue."
            ),
        )

    if len(plausible) == 1 or plausible[0].score >= plausible[1].score * decisive_ratio:
        leader = plausible[0]
        return Finding(
            streak=streak,
            status=FindingStatus.MATCH,
            candidates=(leader,) if len(plausible) == 1 else plausible[:3],
            message=f"{leader.name} (NORAD {leader.norad_id})",
        )

    return Finding(
        streak=streak,
        status=FindingStatus.AMBIGUOUS,
        candidates=plausible[:4],
        message=(
            f"{len(plausible)} satellites fit this trail comparably well; "
            "the evidence does not separate them."
        ),
    )
