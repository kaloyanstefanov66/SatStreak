# SatStreak

Identify which satellite made the streak in your night-sky photograph.

You point a camera at the sky, leave the shutter open, and a bright line crosses
the frame. `satstreak` takes that photograph and tells you which satellite it was,
with a NORAD ID, a name, and a confidence score — or tells you plainly that it
could not work it out.

> **Status: pre-alpha, and not yet useful.** The scaffolding, data model and test
> suite are in place. Orbit propagation, streak detection and matching are not.
> The feasibility question below has not been answered yet, and the project does
> not work until it is. See [Roadmap](#roadmap).

## How it is meant to work

```console
$ satstreak identify night-sky.jpg
ISS (ZARYA) (NORAD 25544) - confidence 94%
```

1. Read the shutter time, location and exposure from the photograph's EXIF data.
   Any of these can be supplied by hand when the camera did not record them.
2. Plate-solve the image against a star catalogue to recover exactly where the
   camera was aimed and how wide its field of view was. **This requires visible
   stars in the frame.**
3. Detect the streak and measure its position, direction and length in the image.
4. Propagate every catalogued satellite over a window around the shutter time and
   project those that cross the field of view into pixel coordinates.
5. Score each candidate on how well its predicted track matches the observed
   streak, and report the best — or report that nothing fits.

Matching leans on track geometry rather than timing. A clock that is a few
seconds out moves a satellite *along* its path, not off it, so the line a
satellite draws across the sky is far more reliable evidence than the moment it
was at any point on that line. Timing is used to rule candidates out, not in.

## What it will not do

- **Work without stars in the frame.** Plate solving is what makes the geometry
  trustworthy. A photograph with a streak and no stars gets an honest
  "could not determine pointing", not a guess.
- **Pretend to certainty it does not have.** Three separate negative outcomes are
  reported — no pointing recovered, no streak found, and no satellite matched —
  because collapsing them would hide the most common real failure behind the
  least common one. Several satellites fitting equally well is reported as
  *ambiguous* rather than as a winner.
- **Identify aircraft, meteors or lens artefacts.** These also leave streaks. If
  one is in your frame, expect `no_match`.

## Roadmap

The one assumption this project rests on is that ordinary photographs — phone
photographs in particular — can be plate-solved reliably enough to recover
pointing. If they cannot, nothing downstream has any value. So that question is
being answered first, before the machinery that depends on it is built.

| # | Milestone | Status |
|---|-----------|--------|
| 0 | Feasibility: can real night photos be plate-solved? (`spikes/`) | in progress |
| 0b | Upper-bound solve rate from public images, by camera class (`spikes/`) | ready to run |
| 1 | Package skeleton, data model, tests, CI | done |
| 2 | Orbit data: CelesTrak fetch with caching, propagation | not started |
| 3 | Geometry: pointing + observer + time window to pixel-space tracks | not started |
| 4 | Streak detection | not started |
| 5 | Matcher and calibrated confidence score | not started |
| 6 | Real photographs: EXIF, plate-solve backend, measured accuracy | not started |

A hosted web demo and a write-up come after milestone 6, and only if the accuracy
numbers justify them.

## Prior art

SatStreak deliberately does not duplicate these:

- [SatIdentifier](https://github.com/exoplanet5/SatIdentifier) predicts which
  satellites cross a given field of view. It does not analyse images.
- [IAU CPS SatChecker](https://satchecker.cps.iau.org/) and Project Pluto's
  `Sat_ID` are prediction services.
- [ASTRiDE](https://github.com/dwkim78/ASTRiDE) and `reca-streaks` detect streaks
  in astronomical images without matching them to a catalogue.

The gap is a self-contained tool that goes from an ordinary photograph to a named
satellite with a confidence score, in one command.

## Development

```console
python -m pip install -e ".[dev]"
python -m pytest
ruff check . && ruff format --check .
```

## Licence

MIT. Satellite orbital data comes from [CelesTrak](https://celestrak.org/) and
originates with the US Space Force's public catalogue.
