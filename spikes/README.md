# Spikes

Throwaway scripts that answer a question before the real code is written. Nothing
here is imported by the `satstreak` package, nothing here is covered by the test
suite, and all of it is expected to be deleted once the question is settled.

## `platesolve_spike.py` — milestone 0

**Question: can ordinary night-sky photographs be plate-solved reliably enough to
recover where the camera was pointing?**

This is the assumption the whole project rests on. Plate solving is what turns a
photograph into a known patch of sky; without it there is nothing to project
satellite tracks into, and milestones 3 through 5 have no input. Most
astrometry.net success stories involve telescopes and dedicated astro cameras,
not a handheld phone at ISO 3200 with rolling-shutter smear and a city glow. That
is a materially different problem and it has not been tested here.

### What counts as an answer

Run the spike over a batch of real photographs and read the `solve_rate` it
reports. Rough interpretation:

| Solve rate | Reading |
|-----------|---------|
| above ~0.7 | The premise holds. Build milestones 2–5 as planned. |
| ~0.3 to 0.7 | Works on some photographs. The tool is viable but needs to state its requirements bluntly ("tripod, 10s+, wide lens") and fail loudly otherwise. |
| below ~0.3 | The premise does not hold for phone photographs. Either pivot to a manual-pointing mode where the user supplies azimuth and altitude, or narrow the audience to people with proper astro rigs. Decide before writing the matcher, not after. |

### How to run it

Audit metadata first. This uploads nothing and shows you what your camera
actually records:

```bash
python -m pip install -e ".[spike]"
python spikes/platesolve_spike.py photos/ --exif-only
```

Then solve for real. You need a free API key from
<https://nova.astrometry.net/api_help>:

```bash
export ASTROMETRY_API_KEY=xxxxxxxx
python spikes/platesolve_spike.py photos/ --out spike-out/
```

Results land in `spike-out/platesolve-results.json`, one record per image plus a
summary. That file is the evidence; keep it.

### Privacy

Solving uploads your photographs to a third-party server. The script strips all
metadata before upload and marks every submission non-public, but the pixels
still leave your machine. If a photograph is one you would not want on someone
else's disk, do not solve it.

### What to shoot

The point is to sample the range of conditions a real user would be in, not to
produce the best possible photographs. Deliberately include some that should
fail — a spike that only tests the easy case answers nothing.

Aim for roughly 15–25 frames covering:

- **Exposure.** A few at 2–5 s, several at 10–20 s, a few at 30 s. Short
  exposures catch fewer stars, which is where solving should start to break down.
- **Support.** Half on a tripod or propped against something solid, half
  handheld. Handheld frames will be trailed; whether the solver copes is exactly
  what we need to know.
- **Sky.** Some from inside Sofia with full light pollution, some from darker
  outskirts if you can manage it. Light pollution washes out the faint stars the
  solver matches on.
- **Framing.** Mostly sky. A frame that is half rooftop has half the stars.
- **Lens.** If the phone has several, try the main and the ultra-wide. Ultra-wide
  fields are harder to solve and are also what people instinctively reach for.
- **A couple of deliberate failures.** One badly out of focus, one with a
  streetlight in frame.

Keep the originals unedited — no filters, no night-mode compositing if you can
disable it, and do not let anything strip the EXIF. Transfer them off the phone
in a way that preserves metadata (a cable, not a messaging app; chat apps strip
EXIF and recompress).

**Bonus, not required:** a frame containing an actual ISS pass, with the time
noted. That becomes the first end-to-end test case with a known right answer.
Pass times for Sofia are on <https://heavens-above.com>. It is worth getting, but
do not hold up the feasibility answer waiting for a clear night and a good pass —
any satellite streak, or even no streak at all, still tests the solving question.

---

## `corpus_spike.py` — milestone 0, part two

**Question: is there any answer available *before* the photographs exist?**

Partly. This script pulls freely licensed night-sky images from Wikimedia Commons
and runs them through the same solver, producing a solve rate segmented by camera
class.

### Read the number correctly

It is an **upper bound, not an estimate.** Commons images are self-selected — a
photograph is there because somebody thought it good enough for an encyclopedia —
and the script downscales before solving, which suppresses noise. Both biases
push the number up. Nova's own gallery is worse: it lists successes only, and
`/latest`, `/submissions` and `/jobs` all serve the same page, so there is no way
to enumerate what failed.

An upper bound can only give a confident **negative**:

- Phone images solve at ~20% here → stop. Real user photographs will be worse.
- Phone images solve at ~85% here → much less has been learned than it feels
  like. Real photographs are still required before milestones 4 and 5.

The segmentation is what makes it worth running. An aggregate rate is dominated
by tracked DSLR astrophotography and says nothing about phones.

### Two outputs, two purposes

1. **Solve rate by camera class** — the upper bound above.
2. **Matcher ground truth** — files whose title names a specific satellite (ISS
   transits, Starlink trains). These have a known right answer, so they test
   milestone 5 directly and are not affected by selection bias at all. This is
   arguably the more valuable output.

### Before you run it: nova is currently unusable for batches

Measured on 2026-09-22, after request pacing was added so this is not
self-inflicted throttling:

- A **single** image sat in nova.astrometry.net's public queue for **over five
  minutes without being assigned a job at all**, and produced no verdict within
  ten minutes.
- A batch of thirty produced **zero** verdicts in twenty-five minutes.

The public service is fine for solving the occasional image by hand. It cannot
support an evaluation corpus, which milestone 6 has to solve repeatedly. See
decision 10 in `DECISIONS.md`: the solver becomes a swappable backend with a
local implementation, and the choice between a local astrometry.net install and
`tetra3` is still open.

Until that exists, this spike will gather and attribute a corpus correctly but
will report every image as having **no verdict** rather than as having failed to
solve. That distinction is deliberate: not being able to ask is not the same as
being told no, and counting one as the other would deflate the solve rate with
someone else's queue depth.

### Running it

```bash
python spikes/corpus_spike.py --fetch-only          # gather + attribute, no uploads
ASTROMETRY_API_KEY=xxx python spikes/corpus_spike.py --limit 40
```

Writes `corpus-results.json` and an `ATTRIBUTION.md` crediting every author, as
CC BY and CC BY-SA require. Output is gitignored; only deliberately promoted
fixtures should be committed, with their attribution.
