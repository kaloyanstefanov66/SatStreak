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
image with a streak and no stars yields no pointing, and a pointing guessed from
compass or orientation EXIF would be wrong by degrees.

A wrong-but-plausible answer is worse than no answer here, because the user has no
way to tell the two apart. Such an image returns `no_pointing`.

## 3. Three distinct negative outcomes, not one

`no_pointing`, `no_streak` and `no_match` are separate statuses. Collapsing them
would hide the failure most likely to occur in practice — not being able to solve
the image — behind the one users would assume, namely that no satellite matched.

An `ambiguous` status exists for the same reason: several satellites fitting
equally well is a real result, and reporting whichever sorted first would
manufacture confidence that was never earned.

## 4. `predict` answers a different question from `identify`, and says so

Plate-solving an image and listing every satellite that crossed its field is
cheap: the matcher computes it internally to have candidates to score, so exposing
it as `satstreak predict` costs almost nothing. It is also the debugging view
needed while building the matcher.

It must not be presented as identification. A 26 mm-equivalent phone frame covers
about **16% of the visible sky**, which at any instant contains roughly:

| Altitude | Objects above horizon | Inside the frame |
|---------:|----------------------:|-----------------:|
|   340 km |                  ~709 |             ~115 |
|   550 km |                ~1,113 |             ~180 |
|   800 km |                ~1,562 |             ~253 |

(From a ~28,000-object catalogue. Sunlit-and-bright-enough filtering cuts this
substantially, but the output remains a candidate list, not a name.)

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
