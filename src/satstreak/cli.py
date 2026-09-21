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

from satstreak import __version__
from satstreak.types import FindingStatus, IdentifyResult, ImageStatus

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

    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
