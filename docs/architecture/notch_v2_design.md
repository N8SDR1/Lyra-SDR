# Notch v2 — senior-engineering deep dive

**Status:** design review, no code yet.
**Date:** 2026-05-02.
**Builds on:** `notch_filter_audit.md` (the first-pass audit).
**Operator request:** "manual notches don't seem to work as well as
they could." Operator approved deep dive before implementation.

This document is the second-opinion / pre-implementation review. It
covers what the first audit missed, what WDSP actually does
(authoritative reference), the mathematical details of the proposed
design, edge-case stability, performance, and the implementation plan
ready to execute against.

---

## 1. What the first audit got right

- iirnotch's poor stopband shape (only -3 dB at the visible edges)
  is a real problem.
- Operator's "doesn't work as well as could" maps to the soft-skirt
  iirnotch behavior.
- Direction is right: parametric biquad with explicit depth control,
  cascade for sharper response, crossfade on coefficient swap.

## 2. What the first audit missed (this is why the deep dive matters)

### 2.1 WDSP doesn't implement manual notches the way we thought

I assumed WDSP cascades biquads with parametric Q + depth. **Wrong.**
Reading the actual source (`wdsp/nbp.c` — Notched Bandpass) reveals
WDSP integrates manual notches into the **demod's bandpass FIR**
itself:

- Operator places a notch at (center, width).
- WDSP's `make_nbp()` splits the bandpass into multiple sub-passbands,
  *excluding* the notch region from the FIR design.
- `fir_mbandpass()` sums the sub-passband FIRs to produce a single
  FIR with sharp notches **integrated into the bandpass design**.
- The receiver runs ONE FIR convolution per sample (the demod
  bandpass), with notches already baked in at zero additional cost.

### 2.2 Why we can't simply port WDSP's architecture

WDSP's approach is mathematically superior — FIR linear phase,
arbitrarily deep stopbands, no IIR stability concerns. So why not
port it directly?

**Latency.**

A FIR notch's stopband depth and transition bandwidth are bound by
its tap count. Rule of thumb (Hann/Blackman window):

```
taps ≈ 4 × rate / transition_width_hz
```

For a 100 Hz wide notch with a 50 Hz transition (so the visible kill
region is `width + 2×transition = 200 Hz` total) at 48 kHz:

```
taps = 4 × 48000 / 50 = 3840 taps → 80 ms latency
```

For tighter operator-set notches (50 Hz wide), latency gets worse.
**80 ms one-way latency in the audio chain is unacceptable** for a
live receiver — the operator would feel the delay during tuning,
TX → RX transitions, and any interactive response.

WDSP gets away with FIR-integrated notches because the demod FIR
runs at the same length **whether or not** there are notches —
1024-tap default, ~21 ms latency, which is the cost the operator
already pays for the demod bandpass anyway. Adding notches to that
existing FIR is "free."

For Lyra to match WDSP's design, we'd have to either:

- (a) Move notches into the demod (couples DSP to mode-specific
  filters; SSBDemod, CWDemod, AMDemod, FMDemod each grow notch
  awareness). Big refactor.
- (b) Run a giant standalone FIR notch stage (high latency).
- (c) Live with cascaded IIR biquads (current architecture, just
  designed properly).

(a) is the right long-term answer, but it's a v0.1+ project: it
couples the notch system to the demod, requires changes to every
demod class, and complicates RX2 plans where each receiver has its
own notch list. Not a quiet-pass-scope change.

(b) is unacceptable due to latency.

(c) is what we should ship now. Properly-designed parametric
biquads with operator-controllable depth give us 80% of the WDSP
quality at zero latency, and don't block the v0.1 architectural
move.

**Conclusion:** stay with per-notch IIR biquads. Just design them
right.

### 2.3 Why iirnotch is a poor design choice (more rigorous)

`scipy.signal.iirnotch(w0, Q)` produces a 2-pole, 2-zero IIR section
with conjugate **zeros exactly on the unit circle** at `e^(±j·w0)`.
Those zeros give "infinite" attenuation at exactly `w0` — which
sounds great until you notice that the stopband only achieves that
depth at the single point `w0`. Just 1 Hz off-center, attenuation
collapses by tens of dB.

The transfer function near `w0` is approximately:

```
|H(e^(jw))|² ≈ |w - w0|² / [(w - w0)² + (BW/2)²]
```

This is a Lorentzian peak in the **inverse** sense — depth scales
quadratically with frequency offset from `w0`:

| Offset from w0 (Hz) | iirnotch attenuation (dB) | Width=100 Hz |
|---|---|---|
| 0 | ∞ (float32 limit ~80 dB) | "kills it" |
| ±10 | 14 | "audible bleed" |
| ±25 | 6 | "definitely leaky" |
| ±50 (= ±width/2) | 3 | "this is the design point" |
| ±100 | 1 | "essentially passes" |

The operator's expectation when they place a "100 Hz wide notch" is
that the **entire 100 Hz region** is attenuated by some amount they
control. The iirnotch design only delivers full attenuation at one
single point in the middle, with most of the visible region barely
attenuated.

This is the "doesn't work as well as could" complaint, mathematically
characterized.

### 2.4 The right biquad: parametric peaking EQ with negative gain

The Robert Bristow-Johnson Audio EQ Cookbook (industry-standard
reference) defines a "peaking EQ" biquad parameterized by:

- `f0` — center frequency
- `Q` — quality factor (controls width)
- `gain_db` — peak gain in dB

When `gain_db < 0`, the peaking EQ becomes a **notch with controllable
depth**. The transfer function:

```
A     = 10^(gain_db / 40)         // amplitude at peak (negative -> attenuation)
w0    = 2π × f0 / fs
α     = sin(w0) / (2 × Q)

b0 = 1 + α × A
b1 = -2 × cos(w0)
b2 = 1 - α × A
a0 = 1 + α / A
a1 = -2 × cos(w0)
a2 = 1 - α / A

H(z) = (b0 + b1·z⁻¹ + b2·z⁻²) / (a0 + a1·z⁻¹ + a2·z⁻²)
```

At `f0` the magnitude response is exactly `A` — i.e., for
`gain_db = -50`, the response at center is exactly `-50 dB`. **The
depth is what the operator asks for, not whatever the design
happens to deliver.**

The width / Q relationship: for a `-50 dB` notch, the -3 dB-from-peak
points (i.e., the points at `-3 dB above the notch floor`) are at
`f0 ± f0/(2Q)`. So:

```
Q = f0 / (-3dB-from-peak BW)
```

Operator-facing: `width = -3dB-from-peak BW`, so `Q = f0 / width`.
Same parameterization as today's iirnotch use, just with controllable
depth.

For comparison at `width = 100 Hz` and `gain_db = -50`:

| Offset from f0 (Hz) | Peaking-EQ attenuation (dB) | iirnotch (dB) |
|---|---|---|
| 0 | -50 (exact) | -80 (limit) |
| ±10 | -38 | -14 |
| ±25 | -28 | -6 |
| ±50 | -20 | -3 |
| ±100 | -8 | -1 |
| ±200 | -2 | <-1 |

The peaking-EQ version delivers consistent attenuation across the
notch region. Operator's mental model matches.

---

## 3. Stability and edge-case analysis

A senior code review of any biquad has to verify pole locations,
denormal handling, and behavior at parameter extremes. Here's the
analysis:

### 3.1 Pole location vs Q and depth

Poles of the peaking EQ biquad sit at:

```
r = (1 - α/A) / (1 + α/A)        // pole magnitude
θ = ω0                            // pole angle
```

For stability, `|r| < 1`. With `α > 0` and `A > 0`, both `(1+α/A)`
and `(1-α/A)` could be positive, both could be negative. We need:

```
|1 - α/A| < |1 + α/A|     →     α > 0 AND A > 0     ✓ always satisfied
```

So the biquad is **unconditionally stable for all `gain_db < 0`**
and any positive Q. ✓

For very high Q (very narrow notch), `α → 0`, so `r → 1`. Pole
approaches unit circle → marginally stable, ringing for many
samples. Numerical roundoff in float32 could push it outside.

**Stability margin recommendation:** clamp Q ≤ 100 (corresponds to
width ≥ f0/100 — at 1 kHz, minimum width 10 Hz). Below that, float32
roundoff can push poles outside the unit circle.

Lyra currently has `NOTCH_WIDTH_MIN_HZ` configured. We need to make
sure it enforces width ≥ ~max(15 Hz, f0/100) for safety. Will
verify in the implementation.

### 3.2 Denormal numbers in IIR feedback

When a biquad's input goes to zero (or near zero), the IIR feedback
`y[n] = -a1·y[n-1] - a2·y[n-2]` decays exponentially. With float32,
once `|y|` drops below ~1.2e-38, samples become denormal. Denormal
arithmetic on x86 is 100-1000× slower than normal arithmetic — a
20-tap denormal cascade can spike CPU usage 10×.

**Lyra is not currently affected** because the IIR runs on baseband
IQ from a real receiver (always has thermal noise above denormal
threshold), but it's worth noting for code review. If we add a
"silence-during-PTT" or test path with literal zero samples, we'd
need a tiny dither (e.g., add 1e-30 to each sample) to avoid the
denormal trap. Not in scope for v0.0.7.1.

### 3.3 Coefficient swap mid-stream — the ringing problem

When operator drags the notch frequency or width slider, we
recompute coefficients. The IIR state `(y[n-1], y[n-2])` is
preserved across the swap because of `lfilter(zi=...)`. **But the
state was generated by the OLD coefficients**, so it represents an
"intermediate state" that the new coefficients would reach if they
had been running on the same input.

Result: when new coefficients see "wrong" state, the filter output
is a transient (briefly off the ideal output) that decays over the
filter's settling time (~10-20 samples for a high-Q biquad). This
transient is the audible "tick" on every drag step.

The fix is to **crossfade output** between the old filter (running
with old coefficients + old state) and the new filter (running with
new coefficients + a state derived from the new coefficients) over
~5 ms. Full design in §5 below.

### 3.4 Behavior at and beyond Nyquist

If operator places a notch at `f0 ≥ Nyquist - width/2`, the upper
edge of the notch wraps around. iirnotch refuses to design (returns
NaN or raises). Peaking EQ would design something but the result
is meaningless.

**Already handled** by Lyra's `_make_notch_filter`:

```python
max_off = NOTCH_RATE / 2 - 100
offset = max(-max_off, min(max_off, offset))
```

This clamps `eff_freq` to `Nyquist - 100 Hz`. The 100 Hz margin is
generous — leaves room for width up to 200 Hz at the Nyquist edge.
Safe.

### 3.5 DC-blocker mode

For `eff_freq < width/2 + 10 Hz`, Lyra uses a Butterworth high-pass
instead of iirnotch. The peaking EQ also fails near DC (sin(w0) → 0
makes alpha → 0 → all coefficients collapse). **We keep the
DC-blocker fallback** unchanged in v2 — Butterworth high-pass is the
right tool for "kill the carrier at VFO center."

UX note: the operator can't independently set "depth" for the
DC-blocker because Butterworth's stopband depth is determined by
its order. We hardcode 4th order = ~24 dB/octave roll-off; this is
adequate for typical carrier-killing use. If we ever want
operator-controllable DC-block depth, we'd switch to a parametric
Chebyshev or elliptic high-pass (more complex, future work).

---

## 4. Cascade design

The first audit suggested replacing `deep: bool` with `cascade: int`
(1-4 stages). The deep-dive math:

For N cascaded identical biquad stages, the magnitude response in
dB scales linearly with N:

```
|H_total|_dB = N × |H_single|_dB
```

So 2 stages at depth `-25 dB` each produces `-50 dB` total.

**This is exactly equivalent to 1 stage at depth `-50 dB` AT THE
CENTER** — but the off-center behavior differs. Cascaded stages
produce a **sharper transition** because the off-center attenuation
also doubles (approximately):

| Cascade × Depth | Attenuation at 0 Hz | At ±width/4 | At ±width/2 |
|---|---|---|---|
| 1 stage × -50 dB | -50 dB | -38 dB | -20 dB |
| 2 stages × -25 dB each | -50 dB | -42 dB | -28 dB |
| 4 stages × -12.5 dB each | -50 dB | -45 dB | -36 dB |

So cascade lets the operator tune **shoulder steepness** independently
of total depth. More stages = steeper transition (closer to "brick
wall") at the cost of 2× CPU per added stage.

**Default recommendation:**

- New notch defaults to `cascade=2`, `depth_db=-50` (operator gets
  -50 dB at center, -42 dB at ±width/4, -28 dB at ±width/2 — solid
  carrier kill with sane CPU cost).
- "Quick" right-click menu: 1-stage gentle, 2-stage standard,
  4-stage aggressive. Defaults to 2-stage.
- Settings → Notches: full slider control (cascade 1-4, depth -20
  to -80 dB).

### 4.1 Cascade implementation note

Each stage uses **its own independent state**. Cascading the same
state twice in sequence (which is what today's `deep=True` does)
is a 2-stage cascade with shared coefficients but independent IIR
state. Mathematically correct but not tunable per-stage.

In v2, all stages use the same coefficients (depth distributed
equally per stage as `total_depth_db / cascade`). Independent state
per stage. This produces the cascade-vs-single equivalence math
above.

---

## 5. Coefficient-swap crossfade (drag-tick fix)

When a notch parameter changes (frequency, width, depth, cascade),
the existing IIR state belongs to the OLD coefficients. Slamming
the new coefficients onto that state produces a 10-20 sample
transient. Repeated drag updates concatenate transients into an
audible "buzz."

### 5.1 The crossfade math

Maintain two filter instances during the transition: `old` (running
with old coefficients + state) and `new` (running with new
coefficients, freshly initialized state). Process the next K
samples through both, then linearly crossfade:

```python
def process_during_swap(self, iq, k_samples_into_swap):
    N = len(iq)
    out_old = self._old_filter.process(iq)
    out_new = self._new_filter.process(iq)
    # Linear ramp old → new over K samples total.
    ramp = np.linspace(
        max(0.0, 1.0 - k_samples_into_swap / SWAP_LEN),
        max(0.0, 1.0 - (k_samples_into_swap + N) / SWAP_LEN),
        N
    )
    return ramp * out_old + (1.0 - ramp) * out_new
```

`SWAP_LEN = 240 samples` (5 ms at 48 kHz) — same as the AGC fade
choices we made; long enough to mask the transient but short enough
to feel responsive during a drag.

After K samples elapsed, drop `_old_filter` and continue with
`_new_filter` only.

### 5.2 What about back-to-back swaps during a drag?

Drag gestures fire dozens of param updates per second. Each fires a
crossfade. If a new swap arrives mid-crossfade:

- Snapshot the current crossfade output as the "old" reference.
- Build the new filter for the new coefficients.
- Restart the crossfade timer.

This requires the crossfade state to track its own "current filter"
(which may itself be a crossfade-in-progress). Implementation-wise,
the simplest approach: instead of double-filtering, just **rebuild
one filter and apply a cosine fade to its first `SWAP_LEN` output
samples**.

```python
def process(self, iq):
    out = self._filter.process(iq)
    if self._fade_remaining > 0:
        # Apply attack ramp to mask the post-rebuild transient.
        n = min(self._fade_remaining, len(out))
        ramp = np.linspace(
            (SWAP_LEN - self._fade_remaining) / SWAP_LEN,
            (SWAP_LEN - self._fade_remaining + n) / SWAP_LEN,
            n
        )
        # During the fade, OUTPUT is a blend between input (filter
        # bypassed) and the rebuilt filter's output.  As ramp goes
        # 0 → 1, we move from "passthrough" to "fully filtered."
        out[:n] = ramp * out[:n] + (1.0 - ramp) * iq[:n]
        self._fade_remaining -= n
    return out
```

This is **simpler than a true two-filter crossfade** but has a
similar acoustic effect: during the 5 ms after a coefficient swap,
the notch fades in. Operator drags freely with no clicks.

The tradeoff: during the 5 ms, the notch is partially bypassed —
the carrier briefly leaks. Acceptable since drag is operator-active
(they're searching for the right notch position; brief leaks during
the search are fine).

### 5.3 What if param change is small?

If operator nudges width by 2 Hz, the coefficient delta is tiny and
the state mismatch barely produces a transient. Today's code already
has a 4% threshold below which width changes don't trigger a rebuild
(see `set_notch_width_at`). We'll preserve that; rebuilds (and
hence crossfades) only fire when the change is meaningful.

---

## 6. Performance budget

Current per-stage cost (scipy lfilter biquad on 1024 complex
samples):

```
~30 µs per stage (Numpy-optimized, single biquad section)
```

For 4 notches with cascade=2 each: 8 stages × 30 µs = 240 µs / block.
Block budget: 21 ms. Notch chain uses ~1.1% of budget. ✓

Cascade=4 worst case: 16 stages × 30 µs = 480 µs / block ≈ 2.3% of
budget. Still tiny. ✓

Crossfade adds zero steady-state cost — it only runs during the 5
ms following a coefficient swap. Total amortized cost is
negligible.

---

## 7. UX surface

Three controls per notch (was two):

- **Width** (Hz) — already exists, unchanged behavior.
- **Depth** (dB, negative) — NEW. -20 to -80 dB. Default -50.
- **Cascade** (1-4) — NEW (replaces `deep` bool). Default 2.

### 7.1 Right-click menu (operator-facing)

Current: "Width" submenu, "Active" toggle, "Deep" toggle, "Remove."

Proposed: "Width" submenu, "Active" toggle, "Notch profile" submenu
with 4 presets, "Remove."

Notch profiles (one-click choices that hit common operator
intentions):

- **Gentle** — cascade=1, depth=-30 dB
- **Standard** ← default — cascade=2, depth=-50 dB
- **Strong** — cascade=2, depth=-70 dB
- **Surgical** — cascade=4, depth=-50 dB (sharper shoulders)
- **Custom...** — opens Settings → Notches at the current notch's
  detail panel, where operator gets full slider control.

### 7.2 Settings → Notches tab

Per-notch detail panel listing all current notches, with sliders
for: frequency, width, depth, cascade. Plus "Default for new
notches" controls so operator can set their preferred one-click
behavior.

### 7.3 Spectrum overlay

- Active notches: filled rectangle (current behavior).
- Inactive notches: greyed (current behavior).
- Cascade indicator: thicker outline, scaled by cascade level (1px
  per level → cascade=4 has 4-pixel outline). Replaces the current
  binary "deep adds 1px" rendering.
- Depth indicator: rectangle FILL color saturation maps to depth
  (lighter for shallow, darker for deep). Subtle but informative.

### 7.4 `notch_details` API tuple shape

Currently: `(freq_hz, width_hz, active, deep)` — 4-tuple.
v2: extend to `(freq_hz, width_hz, active, deep, depth_db, cascade)`
— 6-tuple. UI consumers need to handle either shape during the
migration.

Spectrum widget already accepts 3-tuples (no `deep`) per the comment
in `spectrum.py`. We extend the same tolerance pattern to handle
4-, 5-, and 6-tuples gracefully.

---

## 8. Implementation plan (concrete steps, in execution order)

### Step 1: NotchFilter rewrite (`lyra/dsp/demod.py`)

- Constructor: take `depth_db: float = -50.0`, `cascade: int = 2`,
  drop `deep: bool` (or accept and translate for back-compat).
- DC-blocker path unchanged.
- Off-DC path: replace `iirnotch` with parametric peaking EQ
  formulas above.
- N independent biquad-state pairs allocated for `cascade=N`.
- Add `update_coeffs(freq_hz, width_hz, depth_db, cascade)` method
  that recomputes coefficients in place AND triggers the post-
  rebuild fade-in.
- Add `_fade_remaining` state and apply the dry/wet ramp to the
  first SWAP_LEN samples after each `update_coeffs`.

### Step 2: Notch dataclass (`lyra/radio.py`)

- Replace `deep: bool` with `depth_db: float`, `cascade: int`.
- Migrate the `deep` API surface: `n.deep` becomes a derived
  property returning `cascade > 1`. Existing UI code keeps working.
- `notch_details` returns 6-tuple; spectrum widget tolerates the
  extended shape.

### Step 3: Radio's notch management

- `_make_notch_filter(abs_freq_hz, width_hz, depth_db, cascade)` —
  replace the `deep: bool` arg with the two new ones. Existing
  callers pass legacy `deep=True/False` mapped to
  `(cascade=2, depth=-50) / (cascade=1, depth=-30)`.
- `add_notch` accepts optional `depth_db` and `cascade` kwargs;
  defaults from `_notch_default_depth_db` and
  `_notch_default_cascade`.
- New setters: `set_notch_depth_db_at`, `set_notch_cascade_at`.
- Keep `set_notch_deep_at` as a thin wrapper over `set_notch_cascade_at`
  for back-compat.
- `_rebuild_notches` carries through new fields.

### Step 4: Bench tests

- **Frequency response measurement** — sweep 20 Hz to 24 kHz
  through old vs new at matched configurations. Plot to confirm
  the peaking EQ delivers operator-promised depth.
- **Cascade math validation** — verify N×(depth/N) ≈ 1×depth at
  center, and shoulders sharpen with N as predicted.
- **Drag-tick test** — programmatically tick width by 2 Hz / 50 ms
  for 10 sec, capture audio, confirm no audible click at swap
  boundaries.
- **CPU benchmark** — 4 notches × cascade=2 vs 4 × cascade=4,
  measure per-block overhead.

### Step 5: UI exposure

- Right-click menu: replace "Deep" toggle with "Notch profile"
  submenu (Gentle / Standard / Strong / Surgical / Custom).
- Settings → Notches tab: per-notch detail panel with all sliders.
- Spectrum overlay: cascade-thickness outline + depth-saturation fill.

### Step 6: Help docs + CHANGELOG

- Update `docs/help/notches.md` with depth and cascade explanation.
- CHANGELOG entry for v0.0.7.1 quiet+polish.

### Step 7: Operator flight test

- Same setup as v0.0.7.1 audio-pop testing (LSB / 192k / dummy load
  + on-air bands).
- Place several notches with different profiles, verify behaviour
  feels right.
- Confirm no clicks during drag.

---

## 9. What's explicitly NOT in scope for v0.0.7.1 notch v2

- **WDSP-style FIR-integrated notches** — see §2.2; this is a
  v0.1+ architectural change (couples notch system to demod, RX2
  implications). Tracked as future work but out of scope now.
- **Per-stage depth tuning** — each cascade stage gets identical
  coefficients today. Per-stage tunability would let an operator
  build asymmetric notches (sharper-on-one-side). Not requested,
  not delivered.
- **Linear-phase notches** — IIR has phase distortion across the
  notch. Audibility is sub-perceptual at notch widths the operator
  uses (10-200 Hz). If we ever need linear-phase, the FIR-integrated
  approach in v0.1 covers it for free.
- **Notch presets / band-specific profiles** — "auto-load notches
  for 7.250 MHz to handle the AM broadcast harmonic" is a
  legitimate feature but separate scope.

---

## 10. Open design decisions for operator review

Before implementation, please confirm:

1. **Default depth**: -50 dB is my proposal. Is that right, or do
   you prefer -40 dB (gentler, less likely to ever sound "wrong")
   or -60 dB (more aggressive default)?

2. **Default cascade**: 2 stages (matches today's `deep=True`
   default). Confirm OK, or prefer cascade=1 default with operator
   opt-in for sharper.

3. **"Notch profile" submenu names**: Gentle / Standard / Strong /
   Surgical. Or different naming?

4. **Crossfade approach**: dry-wet ramp after rebuild (simpler,
   what I propose) vs true two-filter crossfade (purer
   mathematically but more code). I'd ship the simpler one.

5. **Settings tab depth slider range**: -20 to -80 dB. OK?

6. **Cascade range**: 1 to 4. Or wider?

7. **Help-doc-style strategy**: detailed for operators (similar to
   nr.md) or terse?

Once you approve / amend, I'll execute the implementation plan in
§8 in order, with bench tests at each step.

---

## 11. Approval gate

Before any code change:

1. Operator confirms the WDSP-architecture analysis in §2 and
   agrees we're sticking with per-notch IIR for now.
2. Operator approves the design choices (peaking EQ + cascade +
   crossfade) over the alternatives.
3. Operator answers the open questions in §10.
4. We open a feature branch (extends `feature/v0.0.7.1-quiet-pass`
   or new `feature/notch-v2`) and execute.

If anything in this design feels wrong, we revise BEFORE coding.
The original audit's recommendation was approximately right, but
the rigor here is what the senior-engineering pass adds: pole
analysis, stability margin, denormal-number caveat, latency-driven
architecture justification, and a concrete crossfade implementation
that handles back-to-back swaps without state explosion.
