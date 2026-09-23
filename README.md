# SatStreak

[![CI](https://github.com/kaloyanstefanov66/SatStreak/actions/workflows/ci.yml/badge.svg)](https://github.com/kaloyanstefanov66/SatStreak/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Find the satellites hiding in your night-sky photographs.

Point a camera at the sky and leave the shutter open, and there is a fair chance
something crossed the frame while it was open. You will often not have noticed.
`satstreak` sweeps the photograph, finds the trails, and tells you which satellite
made each one — by NORAD ID and name, with a confidence score — or tells you
plainly that it could not work it out.

> **Status: the whole pipeline works.** `scan` reads a photograph's metadata,
> recovers where the camera was aimed from the stars in the frame, finds the
> trails, and names the satellites that made them. Accuracy has been measured
> against synthetic frames built from real orbits, **not** against real
> photographs. Plate solving needs Linux or WSL for now. See [Roadmap](#roadmap).

## What works today

Point it at a photograph, say roughly where you aimed, and it names what it finds:

```console
$ satstreak scan night-sky.jpg --alt 75 --az 285
SyntheticCo TestPhone 1  ISO 1600
read from the photograph: time 2026-09-22T19:24:25+00:00 (from gps),
  position 42.6977, 23.3219, exposure 20s, field 69 deg (from 26mm)
(solved sensor roll: 30.5 deg)
Found 13 trail(s):
  1. STARLINK-36661 (NORAD 67831) - confidence 94%  [275px at 79 deg]
  2. ONEWEB-0331 (NORAD 49198) - confidence 96%     [180px at 167 deg]
  3. EMISAT (NORAD 44078) - confidence 99%          [263px at 179 deg]
  4. KUIPER-00505 (NORAD 68886) - confidence 95%    [234px at 25 deg]
  ...
```

**The time, place, exposure and field of view come from the photograph.** You
only say roughly where you aimed, because that is the one thing a photograph does
not record — and plate solving, which would recover it, is milestone 6.

You are not asked for the camera's rotation either, because nobody knows it.
SatStreak solves for it: above it recovered 30.5 degrees against a true 31.

### Measured accuracy, so far

Against synthetic frames built from real orbits, with the roll unknown:

| | |
|---|---|
| Precision | **100%** — of 12 trails it named, 12 were right |
| Recall, trails at least 25px long | **70.6%** (12 of 17) |
| Recall, every rendered trail | 40% (12 of 30) |

The two recall figures differ because 13 of those 30 "trails" are one to five
pixels long — geostationary and navigation satellites, which barely move during
a 20 second exposure. Not detecting them is correct behaviour, not a miss: a
three-pixel mark cannot be identified, and naming one would be a guess.

**These are synthetic frames.** They carry no lens distortion, no light-pollution
gradient, no cloud and no foreground, so real photographs will do worse. What the
numbers establish is that the geometry and the matching are sound.


`satstreak predict` takes a time, a place and where you aimed, and lists what
crossed the frame. It needs no photograph and no plate solving:

```console
$ satstreak predict --time 2026-09-22T17:20:00+03:00     --lat 42.6977 --lon 23.3219 --elevation 550     --alt 85 --az 180 --fov 70 --exposure 20
27 satellite(s) in frame at 2026-09-22T14:20:00+00:00
  note: 70 deg field - positions near the corners are approximate, as the
  tangent-plane model ignores lens distortion

  STARLINK-1246          NORAD 45235   alt  88.4 deg  arc 18.53 deg  trail 1136px at  37.3 deg
  NAVSTAR 43 (USA 132)   NORAD 24876   alt  87.8 deg  arc  0.17 deg  trail   10px at 118.4 deg
  STARLINK-36851         NORAD 67943   alt  86.6 deg  arc 16.42 deg  trail  985px at   6.3 deg
  ONEWEB-0011            NORAD 44062   alt  80.6 deg  arc  6.72 deg  trail  390px at  91.1 deg
  CXO                    NORAD 25867   alt  80.2 deg  arc  0.05 deg  trail    3px at  13.5 deg
```

The spread is a good sanity check on the maths: Starlink at ~550 km draws a
thousand-pixel trail across a 20 second exposure, the navigation satellites at
~20,000 km barely move, and Chandra on its long elliptical orbit is almost a
point.

This is **prediction, not identification** — it says what *could* be in a frame,
not what is. Identifying an actual trail is what `scan` will do.

## How it is meant to work

```console
$ satstreak scan night-sky.jpg
Found 2 trail(s):
  1. ISS (ZARYA) (NORAD 25544) - confidence 94%  [812px at 37 deg]
  2. unidentified - no catalogued satellite fits  [96px at 154 deg]
```

You do not tell it where to look, and you do not need to have spotted anything.

1. Read the shutter time, location and exposure from the photograph's EXIF data.
   Any of these can be supplied by hand when the camera did not record them.
2. Plate-solve the image against a star catalogue to recover exactly where the
   camera was aimed and how wide its field of view was. **This requires visible
   stars in the frame.**
3. Sweep the whole frame for trails, including faint ones invisible on a phone
   screen. An image may yield none, one, or several.
4. Propagate every catalogued satellite over a window around the shutter time and
   project those that cross the field of view into pixel coordinates.
5. Score each candidate against each detected trail, and report the best — or
   report that a trail matched nothing.

Matching leans on track geometry rather than timing. A clock that is a few
seconds out moves a satellite *along* its path, not off it, so the line a
satellite draws across the sky is far more reliable evidence than the moment it
was at any point on that line. Timing is used to rule candidates out, not in.

## What it will not do

- **Work without stars in the frame.** Plate solving is what makes the geometry
  trustworthy. A photograph with a trail and no stars gets an honest
  "could not determine pointing", not a guess.
- **Pretend to certainty it does not have.** Because the tool volunteers findings
  rather than confirming ones you already spotted, you have no independent way to
  check its answer — so a confident false positive is worse than a miss. Trails
  that match nothing are reported as *unidentified* rather than forced onto the
  nearest satellite, and several satellites fitting equally well is reported as
  *ambiguous* rather than as a winner.
- **Tell an aircraft from a meteor.** Both leave trails, as do cosmic ray hits,
  hot pixels, power lines and lens flare. Anything that is not a catalogued
  satellite comes back as `unidentified`; SatStreak does not claim to say which
  kind of thing it was.
- **Identify satellites that appear as points rather than trails.** The trail is
  the evidence. See [DECISIONS.md](DECISIONS.md) for why, and for the one case
  (geostationary objects) where this may change.

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
| 2 | Orbit data: CelesTrak fetch with caching, propagation | done |
| 3 | Geometry: sky-to-sensor projection, plus `satstreak predict` | done |
| 4 | Trail detection across the whole frame | done |
| 5 | Matcher, validated end to end against synthetic frames | done (score not yet calibrated) |
| 5b | Automatic sensor-roll solving | done |
| 6a | EXIF: time, position, exposure and field of view from the photograph | done |
| 6b | Plate solving: recover the aim from the stars in the frame | done (Linux/WSL) |
| 6c | Measured accuracy on **real** photographs | not started |
| 7 | Windows-capable solver backend, so the CLI needs no WSL or Docker | not started |

Milestone 6 reports **two** numbers, not one. *Detection precision and recall*
says how often a reported trail is real and how many real trails were missed;
*match accuracy* says how often a detected trail was correctly named. A tool that
names trails well but invents one photograph in five is not usable, and a single
combined figure would hide that.

`satstreak predict` will list every satellite that crossed a solved image's field
of view. That is **prediction, not identification** — a phone frame covers about
16% of the visible sky, and measurement against the live catalogue puts of order
100 catalogued objects inside one at any moment. It answers what *could* be in
the picture, and exists mainly because the matcher computes it anyway.

A hosted web demo and a write-up come after milestone 6, and only if the accuracy
numbers justify them.

Design decisions and the reasoning behind them are recorded in
[DECISIONS.md](DECISIONS.md).

## Prior art

SatStreak deliberately does not duplicate these:

- [SatIdentifier](https://github.com/exoplanet5/SatIdentifier) predicts which
  satellites cross a given field of view. It does not analyse images.
- [IAU CPS SatChecker](https://satchecker.cps.iau.org/) and Project Pluto's
  `Sat_ID` are prediction services.
- [ASTRiDE](https://github.com/dwkim78/ASTRiDE) and `reca-streaks` detect streaks
  in astronomical images without matching them to a catalogue.

The gap is a self-contained tool that goes from an ordinary photograph to a named
satellite with a confidence score, in one command, without being told where to
look.

## Platform support

The core — catalogue, propagation, trail detection, matching — is pure Python
and runs anywhere. Only plate solving is platform-constrained, so it is an
optional extra rather than a core dependency:

```console
pip install satstreak                      # core, every platform
pip install satstreak[solver-astrometry]   # local solving, Linux and macOS
```

On Windows the local solver currently needs WSL or Docker. The hosted demo
needs nothing at all, since the solver runs server-side. A natively
Windows-capable backend is milestone 7 — a commitment, not a maybe.

## Development

```console
python -m pip install -e ".[dev]"
python -m pytest
ruff check . && ruff format --check .
```

## Licence

MIT. Satellite orbital data comes from [CelesTrak](https://celestrak.org/) and
originates with the US Space Force's public catalogue.
