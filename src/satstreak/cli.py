"""Command-line entry point.

Intentionally thin: it parses arguments, calls the library, and prints the
result. All behaviour worth testing lives in the library, so that the web demo
built later can call exactly the same function the CLI does.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone

from satstreak import __version__
from satstreak.types import FindingStatus, IdentifyResult, ImageStatus, Observation

#: Shell exit codes. Success means at least one streak was confidently named.
#: A photograph containing nothing is a perfectly good outcome for the user but
#: is not a match, so it is reported as such rather than as success.
EXIT_MATCH = 0
EXIT_NO_MATCH = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="satstreak",
        description="Find the satellites hiding in a night-sky photograph.",
    )
    parser.add_argument("--version", action="version", version=f"satstreak {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan",
        help="sweep a photograph for satellite trails and identify what it finds",
    )
    scan.add_argument("image", help="path to the photograph")
    scan.add_argument(
        "--time",
        dest="timestamp",
        help="shutter time as an ISO 8601 string with offset, overriding EXIF",
    )
    scan.add_argument("--lat", type=float, help="observer latitude, degrees, overriding EXIF")
    scan.add_argument("--lon", type=float, help="observer longitude, degrees, overriding EXIF")
    scan.add_argument("--elevation", type=float, default=None, help="observer height, metres")
    scan.add_argument("--exposure", type=float, default=None, help="shutter duration, seconds")
    scan.add_argument("--json", action="store_true", help="write the result as JSON")

    predict = sub.add_parser(
        "predict",
        help="list the satellites that crossed a patch of sky (prediction, not identification)",
    )
    predict.add_argument("--time", dest="timestamp", required=True, help="ISO 8601 with offset")
    predict.add_argument("--lat", type=float, required=True, help="observer latitude, degrees")
    predict.add_argument("--lon", type=float, required=True, help="observer longitude, degrees")
    predict.add_argument("--elevation", type=float, default=0.0, help="observer height, metres")
    predict.add_argument("--exposure", type=float, default=None, help="shutter duration, seconds")
    predict.add_argument("--alt", type=float, default=None, help="frame centre altitude, degrees")
    predict.add_argument("--az", type=float, default=None, help="frame centre azimuth, degrees")
    predict.add_argument("--roll", type=float, default=0.0, help="sensor rotation, degrees")
    predict.add_argument("--fov", type=float, default=None, help="frame width, degrees")
    predict.add_argument("--width", type=int, default=4000, help="image width in pixels")
    predict.add_argument("--height", type=int, default=3000, help="image height in pixels")
    predict.add_argument(
        "--min-altitude",
        type=float,
        default=10.0,
        help="ignore satellites below this altitude, degrees",
    )
    predict.add_argument("--group", default="active", help="CelesTrak group to load")
    predict.add_argument("--limit", type=int, default=25, help="how many to print")
    predict.add_argument("--json", action="store_true", help="write the result as JSON")
    return parser


def _render(result: IdentifyResult, as_json: bool) -> str:
    if as_json:
        return json.dumps(result.to_dict(), indent=2)

    if result.status is not ImageStatus.FOUND:
        return result.message or result.status.value

    lines = [f"Found {len(result.findings)} trail(s):"]
    for index, finding in enumerate(result.findings, start=1):
        streak = finding.streak
        where = f"{streak.length_px:.0f}px at {streak.angle_deg:.0f} deg"
        if finding.status is FindingStatus.MATCH:
            best = finding.best
            assert best is not None
            lines.append(
                f"  {index}. {best.name} (NORAD {best.norad_id}) "
                f"- confidence {best.score:.0%}  [{where}]"
            )
        elif finding.status is FindingStatus.AMBIGUOUS:
            names = ", ".join(f"{c.name} {c.score:.0%}" for c in finding.candidates[:3])
            lines.append(f"  {index}. ambiguous: {names}  [{where}]")
        else:
            lines.append(f"  {index}. unidentified - no catalogued satellite fits  [{where}]")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "scan":
        # Milestones 2-6 replace this with a real call into the library. Until an
        # image can actually be plate-solved there is no honest answer to give,
        # so the placeholder reports the failure it would really hit rather than
        # inventing a finding.
        result = IdentifyResult(
            status=ImageStatus.NO_POINTING,
            message=(
                "Not implemented yet: satstreak cannot plate-solve an image at this "
                "stage, so the camera's pointing is unknown."
            ),
            diagnostics={"image": args.image},
        )
        print(_render(result, args.json))
        return EXIT_MATCH if result.matched else EXIT_NO_MATCH

    if args.command == "predict":
        return _predict(args)

    return EXIT_ERROR


def _predict(args) -> int:
    from satstreak.geometry import Pointing
    from satstreak.predict import predict as run_predict

    try:
        when = datetime.fromisoformat(args.timestamp)
    except ValueError:
        print(f"Could not parse --time {args.timestamp!r} as ISO 8601", file=sys.stderr)
        return EXIT_ERROR
    if when.tzinfo is None:
        # Rather than guess, say so: a local time read as UTC rotates the sky by
        # the observer's offset and would quietly produce a wrong answer.
        print(
            "--time needs a UTC offset, e.g. 2026-09-21T23:14:07+03:00",
            file=sys.stderr,
        )
        return EXIT_ERROR

    observation = Observation(
        timestamp=when.astimezone(timezone.utc),
        latitude_deg=args.lat,
        longitude_deg=args.lon,
        elevation_m=args.elevation,
        exposure_s=args.exposure,
    )

    pointing = None
    if args.alt is not None and args.az is not None:
        if args.fov is None:
            print("--fov is required when --alt and --az are given", file=sys.stderr)
            return EXIT_ERROR
        pointing = Pointing(
            altitude_deg=args.alt,
            azimuth_deg=args.az,
            roll_deg=args.roll,
            scale_arcsec_per_px=args.fov * 3600.0 / args.width,
            width_px=args.width,
            height_px=args.height,
        )

    results = run_predict(
        observation,
        pointing,
        group=args.group,
        min_altitude_deg=args.min_altitude,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "observation": observation.to_dict(),
                    "count": len(results),
                    "satellites": [
                        {
                            "norad_id": p.norad_id,
                            "name": p.name,
                            "peak_altitude_deg": round(p.peak_altitude_deg, 2),
                            "arc_deg": round(p.track.arc_deg, 3),
                            "elements_are_stale": p.track.elements_are_stale,
                            "pixels": (
                                {
                                    "length_px": round(p.pixels.length_px, 1),
                                    "angle_deg": (
                                        round(p.pixels.angle_deg, 1)
                                        if p.pixels.angle_deg is not None
                                        else None
                                    ),
                                }
                                if p.pixels is not None
                                else None
                            ),
                        }
                        for p in results[: args.limit]
                    ],
                },
                indent=2,
            )
        )
        return EXIT_MATCH

    where = "in frame" if pointing is not None else f"above {args.min_altitude:g} deg"
    print(f"{len(results)} satellite(s) {where} at {observation.utc.isoformat()}")
    if pointing is not None and pointing.is_wide_field:
        print(
            f"  note: {pointing.field_width_deg:.0f} deg field - positions near the "
            f"corners are approximate, as the tangent-plane model ignores lens distortion"
        )
    print()
    for p in results[: args.limit]:
        extra = ""
        if p.pixels is not None and p.pixels.angle_deg is not None:
            extra = f"  trail {p.pixels.length_px:6.0f}px at {p.pixels.angle_deg:5.1f} deg"
        stale = "  [stale elements]" if p.track.elements_are_stale else ""
        print(
            f"  {p.name[:32]:<34} NORAD {p.norad_id:<7} "
            f"alt {p.peak_altitude_deg:5.1f} deg  arc {p.track.arc_deg:5.2f} deg{extra}{stale}"
        )
    if len(results) > args.limit:
        print(f"  ... and {len(results) - args.limit} more")
    return EXIT_MATCH


if __name__ == "__main__":
    sys.exit(main())
