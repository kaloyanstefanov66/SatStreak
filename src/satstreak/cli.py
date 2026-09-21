"""Command-line entry point.

Intentionally thin: it parses arguments, calls the library, and prints JSON. All
behaviour worth testing lives in the library, so that the web demo built later
can call exactly the same function the CLI does.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from satstreak import __version__
from satstreak.types import IdentifyResult, Status

#: Shell exit codes. A confident identification is the only success.
EXIT_MATCH = 0
EXIT_NO_MATCH = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="satstreak",
        description="Identify which satellite made the streak in a night-sky photograph.",
    )
    parser.add_argument("--version", action="version", version=f"satstreak {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    identify = sub.add_parser("identify", help="identify the streak in a photograph")
    identify.add_argument("image", help="path to the photograph")
    identify.add_argument(
        "--time",
        dest="timestamp",
        help="shutter time as an ISO 8601 string with offset, overriding EXIF",
    )
    identify.add_argument("--lat", type=float, help="observer latitude, degrees, overriding EXIF")
    identify.add_argument("--lon", type=float, help="observer longitude, degrees, overriding EXIF")
    identify.add_argument("--elevation", type=float, default=None, help="observer height, metres")
    identify.add_argument("--exposure", type=float, default=None, help="shutter duration, seconds")
    identify.add_argument("--json", action="store_true", help="write the result as JSON")
    return parser


def _render(result: IdentifyResult, as_json: bool) -> str:
    if as_json:
        return json.dumps(result.to_dict(), indent=2)
    if result.status is Status.MATCH:
        best = result.best
        assert best is not None
        return f"{best.name} (NORAD {best.norad_id}) - confidence {best.score:.0%}"
    lines = [result.message or result.status.value]
    lines.extend(f"  {c.name} (NORAD {c.norad_id}) - {c.score:.0%}" for c in result.candidates)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "identify":
        # Milestones 2-6 replace this with a real call into the library. Until a
        # photograph can actually be plate-solved there is no honest answer to
        # give, so the placeholder reports the failure it would really hit rather
        # than inventing a candidate.
        result = IdentifyResult(
            status=Status.NO_POINTING,
            message=(
                "Not implemented yet: satstreak cannot plate-solve an image at this "
                "stage, so the camera's pointing is unknown."
            ),
            diagnostics={"image": args.image},
        )
        print(_render(result, args.json))
        return EXIT_MATCH if result.status is Status.MATCH else EXIT_NO_MATCH

    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
