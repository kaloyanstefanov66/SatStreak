# Decisions

Why SatStreak is built the way it is. Each entry records the reasoning, not just
the conclusion, so that a decision can be revisited on its merits rather than
re-argued from scratch — and so that a decision that was right at the time can be
recognised as wrong once its assumptions change.

---

## 1. Match on track geometry first; use timing only to rule candidates out

A timestamp that is a few seconds wrong moves a satellite *along* its ground
track, not off it. The line a satellite draws across the sky is therefore far more
robust evidence than the moment it occupied any particular point on that line.

Phone EXIF records whole seconds and phone clocks drift, so a two-to-three second
error is the realistic floor rather than a worst case. A scoring function that
weighted position heavily would be dominated by that error.

Timing still earns its place: it excludes satellites that were nowhere near the
field during the exposure. It is a filter, not a discriminator.

## 2. Require visible stars, and refuse to guess pointing

Plate solving against a star catalogue is what makes the geometry trustworthy. An
image with a trail and no stars yields no pointing, and a pointing guessed from
compass or orientation EXIF would be wrong by degrees.

A wrong-but-plausible answer is worse than no answer here, because the user has no
way to tell the two apart. Such an image returns `no_pointing`.

## 3. The tool volunteers findings, so silence must be cheaper than a guess

SatStreak is not told where to look. It sweeps a photograph and reports what it
finds, which may be nothing, or may be several trails the photographer never
noticed. Two consequences follow, and both shape the data model.

**One image yields zero or more independent findings.** A single ranked candidate
list would encode the assumption that the user had already spotted one streak and
wanted it named. `IdentifyResult` therefore holds a tuple of `Finding`s, each
wrapping one `Streak` with its own candidates and its own outcome.

**Precision matters more than recall.** When a caller points at a streak, a wrong
name is a wrong answer they can at least suspect. When the tool volunteers a
finding, the caller has no independent way to tell a real trail from an aircraft,
a meteor, a cosmic ray hit, a hot pixel, a power line or lens flare. A confident
false positive is therefore far more damaging than a miss, and the invariants are
written to make silence the cheaper failure:

- `FindingStatus.UNIDENTIFIED` must carry no candidates at all, so a weak match
  cannot be presented as a hedge the user might read as the answer.
- `FindingStatus.MATCH` requires a single clear leader; a tie is `AMBIGUOUS`.
- `ImageStatus.NO_POINTING` stays distinct from `NO_STREAKS`. Collapsing them
  would hide "could not tell where the camera pointed" — the failure most likely
  in practice — behind "no satellites in your photo", which a user would readily
  believe and never question.

`Streak` carries only pixel geometry and a detection score, with no interpretation.
Keeping the measurement separate from its identification is what allows the
detector to be evaluated on its own, which decision 9 requires.

## 4. `predict` answers a different question from `scan`, and says so

Plate-solving an image and listing every satellite that crossed its field is
cheap: the matcher computes it internally to have candidates to score, so exposing
it as `satstreak predict` costs almost nothing. It is also the debugging view
needed while building the matcher.

It must not be presented as identification. These are **measured**, not estimated:
CelesTrak's `active` group held 15,990 element sets when checked, and propagating
all of them to Sofia gave 666 above 10 degrees altitude — 4.2% of the catalogue.
A 26 mm-equivalent phone frame covers about 16% of the visible sky, so of order
**100 catalogued objects sit inside a single phone frame** at any moment.

An earlier version of this entry estimated ~180 from a 28,000-object figure. That
number is the full catalogue including debris; `active` is a little over half of
it. The measured value is used instead.

Sunlit-and-bright-enough filtering cuts the list substantially, and a further 38%
of what is above the horizon turns out to be geostationary (see decision 6) and so
leaves no trail at all. The output still remains a candidate list, not a name.

`predict` is therefore documented as prediction. Prior art already covers this
ground — SatIdentifier and the IAU CPS SatChecker FOV tool both do it — so it is
plumbing and a convenience, not a differentiator.

## 5. No identification of LEO satellites that appear as points

Rejected, because the streak *is* the evidence. Removing it does not make the
problem harder; it removes the measurement decision 1 rests on. A point has no
direction of travel, so identification falls back on position alone, which means
falling back on the timestamp.

A satellite at 550 km passing overhead moves at roughly **0.8°/s** apparent. A
three-second clock error displaces it by about **2.4°** — and with of order 180
candidates in a phone frame, that neighbourhood contains many wrong answers.
Discriminating would need sub-second timestamp accuracy, which phone EXIF does not
provide.

Note the boundary: the requirement is *some* elongation, not a long streak. A few
pixels of elongation restores direction. The cliff is between "some" and "none".

## 6. Geostationary point-source identification: deferred, not rejected

Every objection in decision 5 inverts for geostationary objects:

- The population is ~900 tracked objects rather than ~28,000.
- A GEO satellite holds a **fixed azimuth and elevation** from a given site, so the
  timestamp sensitivity disappears entirely — it is in the same place all night.
- The signature is unmistakable and is the inverse of the LEO case: on a fixed
  tripod with a long exposure, stars trail while a GEO satellite stays a point.
- They lie along the Clarke belt, a well-defined arc that is a strong prior.
- Measured: of 200 randomly sampled objects above 10 degrees over Sofia, **76
  (38%) had an apparent arc below 0.01 degrees across a 20 second exposure**,
  at a median range of 38,205 km. Geostationary objects are a much larger share
  of the visible sky than intuition suggests. For contrast, Starlink at 540-660
  km swept 12.9-15.2 degrees over the same exposure, or 0.64-0.76 deg/s, which
  corroborates the ~0.8 deg/s overhead figure used in decision 5.

Deferred rather than adopted because GEO satellites are typically magnitude 10–13
and are not visible to a phone camera. Building this would shift the audience from
"anyone with a phone" to "people with a tracking mount and a real sensor".

Revisit after milestone 6, as a deliberate choice about who the tool is for.

## 7. Milestone 0 runs before the milestones that depend on it

The project rests on ordinary photographs being plate-solvable. If they are not,
milestones 3 through 5 have no input and the work is wasted. So feasibility is
measured first, and the decision thresholds were written down *before* any number
was seen (see `spikes/README.md`).

The public-corpus variant produces an upper bound rather than an estimate, because
every reachable corpus is self-selected for quality. An upper bound can only
support a confident negative, and the tooling states this in its own output rather
than only in documentation.

## 8. Credentials never enter the repository

Enforced structurally rather than by intention: `gitleaks` scans the full history
on every push, and the ignore rules cover environment and key files. Scanning only
the current tip would miss the case that actually matters, since a secret removed
in a later commit stays reachable in the git objects and in every fork and clone.

Code that needs a credential reads it from the environment and fails loudly when it
is absent, rather than carrying a fallback that could be committed by accident.

## 9. Milestone 6 reports two numbers, not one

"Accuracy" is ambiguous for a tool that finds its own subjects. Two separate
quantities are reported:

- **Detection precision and recall** — of the trails reported, how many were real,
  and of the real trails present, how many were found.
- **Match accuracy** — of the trails correctly detected, how many were named
  correctly.

They must not be combined. A tool that names detected trails correctly 95% of the
time but hallucinates a trail in one photograph out of five is not usable, and a
single blended figure would conceal exactly that. Reporting them separately also
makes the precision-over-recall preference in decision 3 measurable rather than
merely asserted.

This follows from the framing rather than from taste: an evaluation designed for
"name this streak I found" measures only the second number, and would have scored
the tool well while it was quietly inventing findings.

## 10. The plate-solving backend must be local for evaluation

nova.astrometry.net is fine for solving the occasional image by hand. It is not a
viable backend for evaluating a corpus, and the evidence is direct: a single
submission of one image sat in the public queue for **over five minutes without
being assigned a job at all**, and a batch of thirty produced no verdicts in
twenty-five minutes. This was measured after request pacing was added, so it is
not self-inflicted throttling.

Milestone 6 needs to solve a corpus repeatedly — once per evaluation run, and
again whenever the detector or matcher changes. A backend whose latency is
unbounded and set by other people's load makes that impossible, and would make
the accuracy numbers a function of how busy a third party was that afternoon.

So the solver becomes a swappable backend with a local implementation, which was
already the intention but is now a requirement rather than a preference.

**Chosen for evaluation: the `astrometry` package on PyPI, run on Linux.** It
calls the real Astrometry.net C library and downloads index series on demand, and
it installs natively on `ubuntu-latest`, so the CI evaluation job needs no Docker
layer. Development happens under WSL.

Two costs are accepted rather than discovered later. The package takes a list of
star positions, not an image, so source extraction is ours to write — which is
arguably better, since it makes extraction a testable stage of our own pipeline
instead of a black box. And its wheels currently stop at CPython 3.13, so the
evaluation environment pins below the development interpreter.

**This imposes nothing on end users.** The evaluation backend and the user-facing
backend are separate concerns: what measures accuracy over a corpus is a
development tool. Web demo users install nothing at all, since the solver runs
server-side.

The hosted service keeps one honest use: an independent check on a handful of
images, to catch a local installation that is silently misconfigured.

## 11. Windows is a supported target for the CLI, eventually

The chosen evaluation backend does not run natively on Windows; it needs WSL. It
would be easy to let that quietly become the shape of the product, so it is
written down that it must not.

Everything except plate solving — the catalogue, propagation, trail detection,
matching, the data model — is pure Python and already runs anywhere. Only the
solver is platform-constrained, and it is therefore an **optional extra behind an
interface**, never a core dependency:

```
pip install satstreak                      # core, every platform
pip install satstreak[solver-astrometry]   # local solving, Linux and macOS
```

A Windows-capable backend is a roadmap item, not a maybe. The realistic options
are `tetra3` (pure Python, runs anywhere, but built for star-tracker fields rather
than 70-degree phone frames) or shipping `solve-field` binaries. Which one is
undecided; that there will be one is not.

The consequence for design work happening now: the solver interface must be
written so a second implementation can be dropped in without disturbing anything
above it. That means the interface takes an image and returns pointing, plate
scale and orientation, and exposes nothing specific to how any one solver works.
Until such a backend exists, Windows CLI users need WSL, Docker or the hosted
demo, and the README says so plainly rather than letting them find out at install
time.

## 12. Plate solving is unresolved, and these are the measurements so far

Recorded because the negative results are the useful part, and because the next
attempt should start from them rather than repeat them.

**The solver runs correctly and returns verdicts.** It is not hanging or
misconfigured. What it returns is "no match".

**Index scale restriction is the dominant performance lever.** Loading a single
scale returns a verdict in about 12 seconds; loading all thirteen ran past 240
seconds without returning one at all. The library's own example loads exactly one
scale, which is easy to miss. Any future work must restrict scales.

**Nothing has solved yet**, across: the full ~70 degree frame at several scale
sets, a 35% central crop (~24 degrees) at matched scales, and a 40% crop with all
scales. All returned "no match" rather than failing.

**Every test image is 65 to 100 degrees wide**, median 74. That matters because it
is also the phone range: a phone main camera is about 70 degrees and its
ultra-wide is 100 to 120. Quad matching assumes a gnomonic tangent plane, and
across such fields both projection curvature and lens distortion distort quad
shapes. This remains the leading hypothesis but is **not proven** -- the crop test
that would have isolated it was inconclusive, because the run was killed on wall
clock before it returned.

**Stitched panoramas cannot be solved at all**, and one was in the first test set
without being noticed. A panorama uses a cylindrical or equirectangular
projection; the tangent-plane assumption does not merely degrade, it does not
apply. `satstreak scan` should detect and reject panoramas with a clear message
rather than searching and reporting `no_pointing`.

**The remaining untested hypothesis is the extracted sources themselves.** The
test frames are landscapes, where much of the image is terrain. The brightest 100
sources may include foreground rather than sky. The next attempt should validate
extraction against an image with a **published WCS**, so that "did the solver
fail" and "were those actually stars" can be told apart. Blind retrying without
a reference is what consumed this session.

