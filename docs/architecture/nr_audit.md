# Lyra-SDR Noise-Reduction Audit — 2026-05-02

Read-only senior DSP / SDR audio engineer pass.  Source under
`Y:/Claude local/SDRProject/lyra/dsp/` and reference at
`D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/Project Files/Source/wdsp/`.

This is an analysis pass; no code written.  Findings inform the next
NR-related work (likely overlapping or following v0.0.8 RX2).

---

## 1. Executive Summary — Top 5 Wins by ROI

1. **Switch NR2 to a larger FFT (1024 or 2048 with the SAME hop ratio).**
   Lyra runs MMSE-LSA on a 256-pt / 128-hop frame at 48 kHz
   (`nr2.py:532-533`); WDSP's default is **fsize=4096, overlap=4 →
   1024-sample hop** (`RXA.c:325-326`).  At 48 kHz, Lyra's bin spacing
   is **187.5 Hz** vs WDSP's **11.7 Hz**.  For voice noise reduction
   this is the single biggest unforced loss in audio quality — every
   speech formant gets smeared across 5–10 Lyra bins where WDSP
   resolves them cleanly.  **Effort: ~1 day** (mostly re-validating
   SPP/AEPF tunings + buffer-size handling).  **Value: huge.**

2. **Move the captured-profile noise reference to a dedicated
   frequency-domain Wiener stage.**  Today `nr.py:870` and
   `nr2.py:1080-1082` simply substitute the captured magnitudes/PSD
   for the live tracker output, then run the rest of the pipeline
   as-if it were live.  The captured profile is **stationary** (no
   time variation), so the decision-directed ξ smoothing in NR2
   becomes mathematically wrong — ξ no longer evolves on a moving
   estimate of `λ_d[n]`, so the musical-noise damping mechanism is
   effectively disabled.  A captured profile deserves its own
   optimal estimator (closed-form Wiener with operator-set SNR).
   **Effort: 2-3 days.  Value: high — this is the captured-profile
   differentiator becoming meaningful.**

3. **Implement cyclostationary 60/120 Hz powerline modeling on top
   of the captured profile.**  Powerline buzz is the most-complained-
   about HF noise in N8SDR's actual environment (see CLAUDE.md memory
   note).  The captured profile is currently a single magnitude vector
   that captures the **average** powerline harmonic comb but loses
   the **phase-coherent structure** that AC-mains noise actually has.
   A powerline-aware capture path would be a true differentiator.
   **Effort: 1-2 weeks.  Value: very high — best-in-class HF noise
   feature, no SDR client has it.**

4. **Fix the squelch's per-sample Python loop.**  `squelch.py:287-381`
   runs a Python `for` loop over every audio sample with attribute
   lookups inside.  At 48 kHz this is 48 000 iterations per second of
   pure-Python overhead.  Same pathology in `anf.py:313-338`.  Both
   are 10-30× slower than they need to be and make the chain markedly
   worse on slow hardware.  **Effort: half a day each.  Value:
   latency headroom.**

5. **Fold ANF + NR2 + LMS sliders into a single visible "Noise
   Reduction" panel on the DSP+Audio control row.**  The right-click
   context menus currently hide a lot of controls.  Operators don't
   discover NR2's gain-method picker, AEPF toggle, SPP toggle, or the
   captured-source toggle.  **Effort: 1 day.  Value: discoverability.**

---

## 2. Per-Module Audit

### 2.1 NR1 — Spectral Subtraction (`nr.py`, 993 lines)

**What's there.**  Magnitude-domain subtraction with COLA-exact
Hanning, FFT=256/hop=128 at 48 kHz.  Two parallel noise trackers: a
legacy VAD-gated exponential (default OFF — operator opt-out only)
and an enabled-by-default minimum-statistics ring-buffer tracker
(`_MinStatsTracker`, `nr.py:83-190`).  Operator-facing strength
slider 0..1 with separate anchor sets per tracker
(`STRENGTH_*_PARAMS_MINSTATS`, `nr.py:267-278`).  Captured-profile
capture/load/clear plumbing.

**What works.**  The min-stats tracker is the right call —
`nr.py:316-334` documents the dead-on-arrival bug of the legacy VAD
gate explicitly.  The bias-correction value `BIAS_CORRECTION = 2.5`
(`nr.py:143`) is in the right ballpark.  The capture-without-NR FFT
loop (`nr.py:797-808`) correctly piggybacks capture on the FFT framing.

**What's missing / wrong.**

- **The `_MinStatsTracker` is a simplified ring-buffer minimum, not
  the full Martin (2001) algorithm.**  Lyra's NR1 doesn't use the
  bias-corrected one in `_MartinMinStatsTracker` (which lives in
  `nr2.py:185-469`).  The full Martin tracker has proper variance
  estimation → per-bin Q_eq → per-bin bias correction; Lyra's NR1
  uses one global bias factor.  **Recommendation:** plumb
  `_MartinMinStatsTracker` into NR1 too as a third option; the cost
  is one shared instance class.
- **The `BIAS_CORRECTION = 2.5` and the strength-anchor `beta`
  values are coupled.**  A change to one breaks the perceptual
  calibration of the other.  Document the coupling more loudly or
  lock them together.
- **No spectral-domain smoothing of the gain mask.**  WDSP applies
  AEPF (adaptive frequency-domain smoothing of the gain) inside NR2
  only — NR1 in Lyra has no equivalent, so it produces classic
  musical noise at strength=1.0.  Even a fixed 5-bin median smooth
  would help.
- **The smart-guard CV threshold (0.5 at `nr.py:297`) is operator-
  untunable.**  A power-line carrier with stable amplitude has CV
  well under 0.5, so the guard correctly accepts tonal noise — but
  the threshold is brittle and undocumented to the operator.
  Consider exposing it in Settings → Noise as "capture sensitivity"
  with three discrete steps (Strict / Normal / Permissive).
- **Edge case: `process()` returns `np.zeros_like(audio)` if the
  very first block is shorter than FFT_SIZE (`nr.py:885-890`).**  The
  comment claims this never happens with Radio's 2048-sample blocks,
  but RX2 v0.0.8 may eventually pass smaller blocks — explicit guard
  or unit test recommended.

**UX.**  Right-click menu is functional; the Cap button is well-
placed.  Slider math is sound but the 0..1 range with "barely-on" at
0.0 is unintuitive — operators expect 0 to mean "no NR".  Consider
either renaming to "depth" or shifting the range to 0.1..1.0 with
0 = bypass.

**CPU/latency.**  ~2.7 ms internal latency, single FFT/IFFT per 128
samples = ~375 fps at 48 kHz.  NumPy ops are vectorized correctly.
The min-stats tracker `np.min(buf, axis=0)` over 562×129 floats per
frame is fine but could be made incremental (track min via priority
queue) for ~30% saving — low priority.

### 2.2 NR2 — MMSE-LSA / Wiener (`nr2.py`, 1434 lines)

**What's there.**  Full Ephraim-Malah MMSE-LSA gain function via 2-D
LUT lookup (`nr2.py:1163-1186`), Wiener gain LUT alternative
(`nr2.py:1188-1232`), Speech-Presence-Probability mask (`_apply_spp`,
`nr2.py:1274-1310`), Adaptive Equalization Post-Filter (`_apply_aepf`,
`nr2.py:1312-1389`), full Martin (2001) tracker
(`_MartinMinStatsTracker`, `nr2.py:185-469`), and a captured-profile
path.

**What works.**  This is genuinely competent code.  The decision-
directed ξ smoothing at α=0.98 is correct.  The LUT pre-compute with
bilinear interp is the right structural choice.  The Bessel I0/I1
fallback when scipy isn't present is the right thing for a "pip
install and go" app.  The full Martin tracker port is faithful (lines
337-469 match WDSP `LambdaD` step-for-step).

**What's missing / wrong.**

- **FFT size is too small.**  `FFT_SIZE = 256` (`nr2.py:532`).  WDSP
  runs EMNR at fsize=4096, ovrlp=4 (`RXA.c:325-326`).  At 48 kHz Lyra
  has 187.5 Hz bin spacing; voice F0 (90-180 Hz for males, 165-255
  for females) sits in fewer than 2 bins, and the first formant gets
  blurred across bins.  WDSP's 11.7 Hz bins resolve formants cleanly.
  **This is the single biggest perceptual gap vs WDSP.**  Recommend
  FFT_SIZE=1024 with hop=256 as a compromise (lower memory, half-WDSP
  frequency resolution, latency rises from 2.7 ms to 10.7 ms — still
  inaudible).
- **Captured-profile stationarity breaks decision-directed ξ
  updates.**  Look at `nr2.py:1093`: `ml_estimate = self._prev_clean_
  pow / lambda_ref`.  When `lambda_ref` is the captured profile
  (frozen), the only time-varying input to ξ is `_prev_clean_pow`.
  The musical-noise-killing α=0.98 smoothing relies on a noise
  estimate that **moves with the audio**; with a frozen profile, ξ
  effectively becomes (frozen captured PSD)·(a-priori component) —
  the smoothing now blurs gain in time but the noise reference
  doesn't update.  Net: captured-profile mode is theoretically WORSE
  than live for MMSE-LSA, opposite of the operator's intuition.  The
  fix is to bypass MMSE-LSA when in captured-profile mode and run a
  closed-form Wiener: `gain[k] = max((|Y[k]|² − captured[k]) / |Y[k]|²,
  floor)` with operator-controlled subtraction depth.
- **The Wiener LUT exposes the divide-by-tiny-γ pathology.**
  `nr2.py:1224-1225` clips `gamma_safe = max(γ, 1e-10)` then computes
  `sqrt(v)/gamma_safe`.  At gamma_db = -10 (linear 0.1) and xi_db =
  -25 (linear ~0.003), v = ~0.003, sqrt(v)/γ ≈ 0.55, exp(-v/2) ≈ 1,
  Bessel ≈ 1, so the gain trends to ~0.55 + lower at this extreme.
  Then `np.clip(gain, 0.0, 1.0)` saves us.  This is fine but worth a
  unit test for the corners.
- **AEPF edge handling has a left-edge-only Python loop.**
  `nr2.py:1372-1387` is `for k in range(min(n, msize)):` — that's up
  to 21 iterations at PSI=20, which is OK but Python-loop in a hot
  path.  Replace with a vectorized partial-cumsum for symmetry with
  the interior region.
- **NR2's captured-profile path has a subtle resampling bug.**
  `_captured_lambda_d = np.maximum(arr, 1e-6) ** 2` (`nr2.py:978`) —
  but the floor 1e-6 was applied IN MAGNITUDE DOMAIN by NR1 at
  `nr.py:641`, so the floor in NR2's power domain becomes 1e-12,
  fine.  However if a profile is loaded that came from a DIFFERENT
  FFT size (rejected by NR1's load path) it'll be silently rejected
  at NR2 load too — but if both are at FFT=256 (current default),
  there's no resample logic anywhere.  If/when FFT_SIZE changes,
  every saved profile becomes unloadable.  Recommendation: add an
  FFT-bin-resampling path on profile load (linear interp from
  saved-bin-axis to current-bin-axis).
- **No mode-aware aggression scaling.**  A captured profile from SSB
  at 2.4 kHz BW won't match what NR2 sees in CW at 250 Hz BW (the
  demod-bandpass cuts the noise spectrum differently).  Profile
  metadata stores `mode` (`noise_profile_store.py:90-106`) but
  nothing in NR1/NR2 uses it.
- **`_apply_vad_relax` (`nr2.py:1411-1433`) uses a global frame-mean
  SNR threshold of 6 dB.**  This is the simplest possible VAD and
  will trigger on band-noise transients in HF reception (lightning
  crashes, strong SSB syllable peaks from elsewhere on the band).  A
  proper VAD looks at modulation envelope coherence or spectral
  entropy, not just RMS.

**UX.**  The aggression knob 0..2 is slightly different from NR1's
0..1; consider unifying.  The musical-noise-smoothing toggle, AEPF
toggle, SPP toggle, and gain-method picker are all hidden in
right-click menus.

**CPU/latency.**  Per block at 192 kHz IQ → 48 kHz audio, NR2 runs
~16 frames per Radio block.  Each frame: one rfft (256→129 bins),
Martin update (~10 vectorized ops × 129 bins), 3 small element-wise
ops, LUT lookup (4 gather + bilinear blend), AEPF cumsum (one
np.cumsum per frame), one irfft.  Estimated ~150 µs per frame, ~2.4
ms per Radio block.  Comfortable budget.

### 2.3 LMS Line Enhancer (`lms.py`, 357 lines)

**What's there.**  Block-LMS with adaptive leakage (Pratt's lidx
walk), faithful port of WDSP `anr.c`.  Block size = delay (16
samples) so weights are frozen within a block but per-sample outputs
are vectorized via NumPy gather + dot.

**What works.**  This is a well-engineered port.  Block-LMS at B=16
is a clean speed-up vs per-sample LMS; the analytical justification
at `lms.py:266-277` is correct.  Compounded leakage `(1 - 2μγ)^b` is
the right move (`lms.py:351`).  Strength slider math is monotonic
and lands on Pratt's defaults at 0.5.

**What's missing / wrong.**

- **No mode-awareness.**  LMS in CW = good; LMS in AM = chops the
  carrier; LMS in FM = ugly.  Channel doesn't gate `_lms.process()`
  by mode (`channel.py:832`).  The LMS stays "on" if the operator
  enabled it, regardless of mode.  A simple `if mode in
  {"CWU","CWL","LSB","USB","DIGL","DIGU"}` gate around the LMS call
  would prevent operator surprise.
- **Strength slider doesn't expose taps or delay.**  Operators with
  weak CW DX experience often want longer taps (up to 256) and longer
  delays (up to 64) for stable CW lock.  Today these are fixed at
  `DEFAULT_TAPS=64` and `DEFAULT_DELAY=16` (`lms.py:139-140`).  A
  "Custom" right-click menu like ANF has would solve this.
- **No diagnostic readout.**  Operator can't see whether LMS has
  converged, what the lidx is doing, or what the prediction power vs
  error power ratio is.  WDSP doesn't either, but Lyra has the
  headroom to add one (it'd be one additional `prediction_db`
  property reading off the last block).

**CPU/latency.**  ~0.33 ms internal latency.  NumPy gather + dot per
block; very efficient.

### 2.4 ANF (`anf.py`, 352 lines)

**What's there.**  Leaky-LMS adaptive notch with N_TAPS=64, DELAY=10,
GAMMA=0.10 — clean-room from public DSP literature, with appropriate
WDSP-pattern attribution.

**What works.**  Algorithm is correct.  Profile presets (light/medium/
heavy with mu=5e-5..4e-4) are operator-validated.

**What's missing / wrong.**

- **Per-sample Python loop** at `anf.py:313-338`.  This is the same
  pathology I called out as a top-5 win.  At 48 kHz it's 48 000
  Python iterations/sec with attribute lookup.  The `np.dot(w,
  window)` inner is vectorized but the outer loop is not.  **Fix:**
  there's a known vectorized block-LMS form for adaptive notching
  (the LMS module has it!).  Apply the same block-LMS structure here
  with B = DELAY = 10.  Gives a ~10× speedup with the same algorithm.
- **No notch-frequency observability.**  The operator can't see WHICH
  tones the ANF has learned.  The weight vector's spectrum reveals
  this — a single `numpy.fft.rfft(w, 64)` per second gives a "current
  notches" display.
- **No mode-aware enable.**  Same observation as LMS.

**UX.**  Profile presets are sensible.  "Heavy" briefly nulls vowel
formants (`anf.py:133-135`) — this is documented but operators won't
read the source.

### 2.5 Noise Blanker (`nb.py`, 472 lines)

**What's there.**  IQ-domain detect-then-replace impulse blanker,
pre-decimation.  Background tracker via scipy `lfilter`, threshold
compare, np.maximum.accumulate forward-fill, cosine slew at edges,
consecutive-blank cap.

**What works.**  This is a well-designed module.  Pre-decimation
placement is correct (`nb.py:58-68`).  The forward-fill via
`np.maximum.accumulate` is elegant and O(N).  Cosine slew at
boundaries handles the ringing problem cleanly.  Consecutive-blank
cap prevents lock-on.

**What's missing / wrong.**

- **Single-threshold detector misses sub-impulse-amplitude noise
  events.**  WDSP has SNB ("Spectral Noise Blanker", `wdsp/snb.c`,
  862 lines) which does FFT-domain impulse detection — catches
  things that look impulsive in spectrum but not in time.  Lyra has
  no equivalent.  SNB would be a meaningful addition for power-line
  crash noise that's continuous-but-bursty.
- **`_enforce_blank_cap` uses a Python loop** (`nb.py:386-400`).  At
  192 kHz × 25 ms cap × bursty noise this could be 2-3k iterations.
  Vectorizable with `np.diff` + cumsum tricks but lower priority —
  the loop only runs when impulses are detected, so cost is bounded
  by activity.
- **No metric exposed.**  "How many impulses have been blanked in the
  last second?" would help operators tune threshold.

**UX.**  Profiles light/medium/heavy with thresholds 12 / 6 / 3 are
sensible.

### 2.6 Squelch (`squelch.py`, 394 lines)

**What's there.**  RMS-window + asymmetric noise-floor tracker +
hysteresis gate + cosine attack/release ramp.

**What works.**  The asymmetric tracking with floor-up only when gate
is closed (`squelch.py:324-329`) is clever and right — it's the
"speech doesn't pollute the floor" insight.  Hang time bridges
syllable pauses.  Cosine ramps prevent clicks.

**What's missing / wrong.**

- **Per-sample Python loop** (`squelch.py:287-381`).  Same pathology.
  Hot path runs the gate state machine per sample at 48 kHz.  The
  whole thing can be re-expressed as: vectorized RMS window via
  cumulative sum, vectorized asymmetric exponential floor via lfilter
  with branch, vectorized hysteresis state via `np.diff` on the
  threshold-cross mask, then cosine ramp via lookup-table convolution.
  Not trivial but ~3 days of work for ~20× speedup.
- **No mode-aware threshold scaling.**  The same threshold value
  (operator-set) is applied regardless of mode.  CW operators want a
  much tighter gate than SSB operators; FM has the carrier giving it
  different floor characteristics.  Mode-keyed thresholds with a
  master "tightness" knob would be more useful.
- **The `is_passing` indicator** (`squelch.py:165-167`) is a public
  API but I see it bound in panels.py — good.  No way to see WHY the
  gate closed (was it ratio-below-close, or hang-elapsed?).
  Diagnostic-only feature, low priority.

**UX.**  RMS-based design is correct given the AM-broadcaster
harmonic interference at N8SDR's QTH (CLAUDE.md memory).  Good
defensible design choice.

---

## 3. Chain-Level Audit

The chain in `channel.py:802-857` is:

```
IQ → NB → decimate → notches (manual) → demod → ANF → SQ → LMS → NR(1 or 2) → APF
```

**The squelch placement is intentionally non-standard.**
`channel.py:818-826` notes Lyra puts SQ BEFORE NR (WDSP puts it
after) on the rationale that NR-smoothed audio confuses voice
detection.  **This is correct.**  But it means the squelch is also
gating the LMS / NR processing, which has implications:

- **When the squelch is closed, downstream LMS/NR/APF see silence.**
  Both `LineEnhancerLMS.process` and the NRs early-return if
  `enabled` is False (good).  But when enabled with silent input,
  LMS's adaptive-leakage walk will drift toward steady-state, NR2's
  `λ_d` tracker will track DOWN (silent ≈ noise floor), and the
  captured profile is unaffected.
- **When the gate opens, LMS weights are stale.**  The first ~half-
  second after gate-open will have LMS untrained on the new signal.
  This is briefly audible.  The current design rebuilds LMS state on
  enable transition (`channel.py:572-575`) but not on gate-open.

**Recommendation:** add a "LMS reset on squelch-open" linkage if
LMS+SQ are both enabled.  Cheap.

**Other chain observations:**

- ANF feeds LMS feeds NR.  ANF's output (residual = input − predicted
  tone) is a **sharper** signal than its input.  LMS then looks for
  periodic content; ANF's residual has had periodic content removed,
  which is exactly the WRONG side for LMS's predictor.  Operators
  using LMS+ANF together will get less effect from LMS than they'd
  expect.  The natural order is **LMS → ANF → NR** (LMS first,
  lifting tones; ANF then removes any residual whistles; NR cleans
  up broadband).  Today's order isn't catastrophic but it's not
  optimal either.

- The chain isn't operator-configurable.  WDSP also hardcodes order,
  so this isn't a regression — but as a differentiator, an "Advanced
  → Chain Order" Settings panel that lets the operator drag-reorder
  NR / ANF / LMS would be unique to Lyra.

- Mode change correctly resets all stages (`channel.py:301-319`).
  Sample-rate change resets NB and rebuilds the decimator
  (`channel.py:285-299`).

---

## 4. Captured-Noise-Profile Audit

This is the signature feature, so this section is the longest.

### 4.1 What's currently there (correctly implemented)

- **Capture path** in `nr.py:523-561`: clamps to [0.5, 30] sec,
  accumulates float64 magnitudes per FFT frame, fires done-callback
  at completion.
- **Capture-without-NR path** (`nr.py:797-808`): operator can capture
  while NR is OFF — important; lets them A/B with profile loaded.
- **Capture during NR2** via `feed_capture()` shim (`nr.py:669-704`,
  called from `channel.py:847`).  NR1 owns the capture state machine;
  NR2 piggy-backs.
- **Smart-guard** via coefficient of variation of frame-power
  (`nr.py:735-763`).  CV > 0.5 → "suspect".
- **Persistence** (`noise_profile_store.py`, 537 lines): atomic JSON
  writes via `tempfile.mkstemp` + `os.replace`, schema versioning,
  sanitized filenames, list/load/save/delete/rename/export/import.
- **Atomic NR1+NR2 load** (`channel.py:414-452`): if NR2 load fails
  after NR1 succeeds, NR1 is rolled back.  Defensive design.

### 4.2 Current weaknesses

**a) Single-resolution profile.**  Profile is one 129-bin (FFT=256)
magnitude vector.  This locks profiles to NR's current FFT_SIZE.
Recommendation #1 in §1 (move to 1024 FFT) breaks every existing
profile.

**b) No frequency context.**  Profile stores `freq_hz` and `mode` but
the noise spectrum captured is post-decimation, post-demod, audio-
domain.  A profile captured on USB at 7.250 (where N8SDR has the 5th
harmonic AM intrusion) is conceptually different from a profile
captured on DIGU at 7.074.  Today they're treated identically.

**c) The captured profile is mathematically incompatible with NR2's
MMSE-LSA pipeline.**  Detailed in §2.2 — frozen `λ_d` defeats the
decision-directed update.  Operator using captured + NR2 gets WORSE
results than captured + NR1, opposite of intuition.

**d) Smart-guard CV threshold is fragile.**  Tonal narrow-band
interference (e.g., a single AM carrier blowing in and out) has
frame-to-frame CV in the 0.3-0.7 range depending on its modulation.
The 0.5 threshold flags some legitimate captures and passes some bad
ones.  **Better heuristic:** decompose the per-frame power series
into mean + variance + spectral kurtosis.  Tonal noise = high
kurtosis; broadband noise = low.  Run both metrics, flag suspect on
EITHER.

**e) Aging is shown but not actionable.**  `noise_profile_manager.py`
colors profiles by age (grey/amber/red) but Lyra never tells the
operator "your loaded profile is now 6 weeks old, recapture
recommended."  A passive check at startup would be one toast
notification per session.

**f) No profile blending.**  `noise_profile_store.py` has no API for
`blend(p1, p2, weight) → p3`.  This is trivially `magnitudes =
w1*p1.magnitudes + w2*p2.magnitudes` (geometric mean would be more
correct for log-magnitude perceptual blending: `exp(w1*ln(p1) +
w2*ln(p2))`).

**g) No auto-suggest.**  When operator changes band/mode, the profile
manager doesn't surface "you have a profile labeled 'Powerline 80m'
and you just tuned to 80m, want to load it?"

**h) FFT-bin-resampling missing on load.**  If FFT_SIZE ever changes,
every saved profile is rejected (`nr.py:633-637`).  Linear
interpolation across bins would unblock the FFT-size upgrade in §1.

### 4.3 Improvement opportunities — evaluated

The user asked for deep evaluation of these.  Each gets DSP-soundness
/ effort / value / risk.

**a) Multi-profile blending / auto-select.**

- DSP: trivial.  Cross-correlation between live noise spectrum and
  library — `argmax_p Σ live[k]·profile_p[k] / (||live||·||profile||)`.
  ~2 ms for 50 profiles.
- Effort: 1 day for blend API + manager UI; 2 days for auto-select +
  Settings toggle.
- Value: high.  Operator can pre-populate a library and Lyra picks
  the best fit per band.  Genuinely differentiating.
- Risk: low.  Fully orthogonal to existing NR.

**STATUS: AUTO-SELECT EXPLICITLY DEFERRED INDEFINITELY (2026-05-02)**

Reviewed and scrapped during P1 work after operator-led
discussion:

> "It's best to let the user switch if he thinks it's a noise
> issue.  The user's ears will pick up things that the interface
> cannot, and each user has different perceived noise issues —
> each station, location and operator is unique.  Great thought,
> just not the right type of application for it."

The captured-noise-profile feature is operator-curated by design —
operators capture profiles that subjectively sound right to them
in the moments they captured.  An algorithmic auto-select reduces
profiles to a spectral-distance metric and overrides operator
choice with a number.  Even a "suggest" mode (toast → click to
apply) creates noise that doesn't deliver value when operator
ears are the better judge.

**What stays in scope going forward:**
- Operator-driven explicit blending UI (pick two profiles + slider →
  save new profile).  Implementation deferred; if/when this lands,
  the blending math is straightforward (geometric mean in log
  space — see audit §4.3(a) original DSP description).
- Diagnostic readouts: e.g. "this profile is X dB different from
  current band noise" displayed in the manager.  Informational;
  operator decides what to do with it.

**Out of scope (do NOT revive without operator request):**
- Algorithmic auto-loading of profiles
- Suggestion toasts that the algorithm initiates
- Any feature where Lyra picks a profile FOR the operator
- The previously-prototyped `lyra/dsp/noise_profile_match.py`
  utility module was removed in the same operator decision —
  zero auto-comparison code in the codebase keeps the principle
  enforced at the file-system level.

**b) Adaptive profile refinement.**

- DSP: sound but needs care.  Slow exp-smoothing of profile toward
  live min-stats: `profile[k] ← (1-α)·profile[k] + α·minstats[k]`
  with α = 1e-4 per frame ≈ 7-minute time constant.  Operator's
  "frozen" mode = α=0; "drift slow" = α=1e-5; "drift fast" = α=1e-3.
- Effort: half a day.
- Value: medium.  Most operators won't move the knob.  But for
  someone running an 8-hour DX session, the band noise floor
  genuinely drifts (sun angle, ionosphere), so this matters.
- Risk: low.  Always-on with α=0 is identical to current behavior.

**c) Cyclostationary 60/120/180 Hz powerline modeling.**  **THE big
differentiator.**

- DSP: real DSP work but well-understood.  Cyclostationary signals
  have correlation between `x[n]` and `x[n+T]` where T = period.
  For 60 Hz at 48 kHz audio → T = 800 samples.  Capture not just
  magnitude per bin but the per-bin **complex** value averaged
  synchronous to the AC-line phase.  Subtraction is then in the
  **complex domain**, removing both magnitude and phase of the
  powerline harmonic comb.  This is mathematically optimal for
  stationary sinusoidal interference — you get cancellation, not
  just spectral attenuation.
- Math sketch: profile becomes `complex_profile[bin, ac_phase_bucket]`
  where `ac_phase_bucket ∈ {0..15}` representing 16 phase positions
  across 16.7 ms.  Each frame, estimate AC phase from a 60 Hz
  reference (frame timestamp mod period), look up the matching
  profile slice, complex-subtract.
- Effort: 1-2 weeks for capture + storage + apply paths.  AC-phase
  reference is the tricky bit — at 48 kHz audio there's no actual
  mains wire to grab phase from, so synthesize from frame counter
  assuming 60 Hz drift is small.  Or operator-tunable AC frequency
  (60 Hz US, 50 Hz EU).
- Value: **enormous** for HF ham noise.  No SDR client (Thetis
  included) does this.  Powerline crud is the #1 HF noise complaint.
- Risk: medium.  Capture-time stationarity assumption is harder
  (need to capture for 1+ second to average AC phase).  Profile size
  grows ~16×.  Backwards-compat schema bump needed.

**d) Time-frequency masking (2D profile).**

- DSP: sound but expensive.  Profile becomes `mag[bin, time_envelope_
  bucket]` where time-envelope is something like local short-time
  power.  Capture: 2D histogram of bin-magnitude given local power
  level.
- Effort: 1 week.
- Value: medium.  Solves "this profile is too aggressive when the
  band is loud" but cyclostationary modeling solves the bigger
  problem first.
- Risk: medium.  Profile size grows 8-16×.

**e) Multi-resolution profiles.**

- DSP: trivial, just multiple captures at different FFT sizes.
- Effort: 2 days.
- Value: medium-low.  Useful only if NR pipeline can use both scales.
- Risk: low.

**f) Wiener-filter-from-profile.**  **Recommended in §1 win #2.**

- DSP: closed-form.  `gain[k] = SNR_post[k] / (1 + SNR_post[k])`
  where `SNR_post[k] = max(|Y|² - profile²[k], 0) / profile²[k]`.
  Operator slider scales the subtracted noise: `SNR_post[k] =
  max(|Y|² - α·profile²[k], 0) / profile²[k]`.
- Effort: 2 days for the new processing stage + UI.
- Value: high — fixes the captured-profile-meets-MMSE-LSA mismatch.
- Risk: low.  Replaces only the captured-profile path; live mode
  unchanged.

**g) Per-band-segment profiles.**  Sub-band profiles (e.g. 7.0-7.1 vs
7.1-7.3 MHz).

- DSP: profiles get an additional `freq_low_hz`, `freq_high_hz`.
  Auto-select (4a) uses the freq window.
- Effort: 1 day on top of (4a).
- Value: medium.  40m has very different noise above/below 7.150 in
  many regions.
- Risk: low.

**h) Signal-conditioned profile lookup.**  Time-of-day / season tags.

- DSP: just metadata.
- Effort: half a day.
- Value: medium.  Would need 6-12 months of operator usage to build
  out useful libraries.
- Risk: low.

**i) Smarter VAD for live-source NR.**

- DSP: spectral entropy + modulation envelope coherence > RMS-only.
  Real ML-free VAD literature has good designs (e.g. Ramirez et al.
  multi-feature VAD).
- Effort: 1 week.
- Value: low if (f) Wiener-from-profile is implemented (operators
  stop using live source).
- Risk: medium — VAD that mis-fires on speech transients is worse
  than no VAD.

**j) Cross-channel profile validation (RX2-aware).**

- DSP: capture on RX2 while RX1 has known clean signal; compute SNR
  estimate from RX1's known-good vs RX2's captured.
- Effort: 1 week, depends on v0.0.8 RX2.
- Value: medium.  Useful for operators who have a known-quiet
  companion radio.
- Risk: low.

**k) Profile auto-staleness detection (live noise vs profile drift).**

- DSP: per-frame `||live_minstats - profile|| / ||profile||`.
  Threshold > 3 dB → "recapture recommended" toast.
- Effort: half a day.
- Value: high — proactive UX win.
- Risk: low.

**l) Profile cross-correlation reporting.**

- DSP: same metric as (k), but as a continuous live readout in the
  manager.
- Effort: half a day on top of (k).
- Value: medium.
- Risk: low.

**m) Spectral-peak-aware smart-guard.**

- DSP: detect peaks in capture-frame magnitudes that aren't in a
  long-term running average.  Tonal noise (powerline harmonic comb)
  has stable peaks → passes the test.  Signal contamination has
  peaks unique to capture window → flag suspect.
- Effort: 1 day.
- Value: medium.  Closes a real corner case.
- Risk: low.

---

## 5. UX Gap Audit

- **Discoverability of captured-profile workflow.**  A new tester
  sees a "📷 Cap" button next to NR.  They need to know to:
  1. Tune to a noise-only frequency
  2. Click Cap (waits 2 sec)
  3. Name the profile
  4. Toggle the source from Live → Captured
  Step 4 is hidden in a right-click menu.  **Recommendation:** after
  a successful capture, auto-toggle the source to Captured and show
  a 3-sec toast "Now using captured profile: <name>".
- **NR2 controls hidden.**  Gain method picker, AEPF toggle, SPP
  toggle, musical-noise-smoothing toggle, speech-aware toggle — all
  in right-click menus or Settings.  A tester won't know NR2 has
  these.  Add an "Advanced" expander in the DSP+Audio panel.
- **No visualization of WHAT the profile looks like.**  Operator
  captures and saves but never sees the spectrum.  A tiny inline
  thumbnail of `magnitudes` (log-Y, semi-transparent) on the manager
  dialog rows would build operator trust.
- **No "current noise" overlay.**  When Cap is loaded, an operator
  can't see how well it matches today's band noise.  A diagnostic
  toggle that overlays `live_minstats` and `loaded_profile` on the
  spectrum painter (say, both as faint ghosted lines) would be a
  power-user feature.
- **Settings → Noise tab organization.**  Today (per `settings_dialog
  .py:NoiseSettingsTab`) it's per-module: NR, NR2, ANF, NB, SQ,
  Captured-profile each get a section.  **Better mental model:**
  organize by "what kind of noise are you fighting":
  1. Impulses / crashes → NB
  2. Whistles / heterodynes → ANF
  3. Broadband hiss → NR1, NR2, captured profiles
  4. Voice gating → SQ
  5. CW lift → LMS
- **Right-click menu on NR.**  Functional but legacy "light/medium/
  heavy" terminology mixed with "Neural" (disabled) and the NR1↔NR2
  split is confusing.  Replace with a 2-row submenu: "Algorithm"
  (NR1 / NR2 / Neural-future) and "Strength" (slider).

---

## 6. Performance Audit

**Per-block estimates at 192 kHz IQ → 48 kHz audio, block = 2048
samples (= 10.7 ms wallclock):**

| Stage | Work | Per-block CPU | Latency |
|---|---|---|---|
| NB (pre-decim) | scipy lfilter on 2048 complex × 2 (I+Q) + np.where masking | ~0.2 ms | 0 (sample-aligned replace) |
| Decimator | scipy lfilter, 257-tap FIR × 2 | ~0.4 ms | ~2.5 ms |
| Notches (per active) | bilinear IIR per notch | ~50 µs each | ~10 µs each |
| Demod (USB) | bandpass + Hilbert mix | ~0.2 ms | ~1.5 ms |
| ANF | **Python loop, 512 samples × ~10 µs each** | **~5 ms** ← BOTTLENECK | ~0.2 ms |
| SQ | **Python loop, 512 samples × ~5 µs each** | **~2.5 ms** ← BOTTLENECK | ~70 ms (cosine ramp) |
| LMS | block-LMS, 32 blocks × NumPy ops | ~0.6 ms | ~0.33 ms |
| NR1 (FFT=256) | 4 frames × rfft+ops+irfft | ~0.4 ms | ~2.7 ms |
| NR2 (FFT=256) | 4 frames × rfft+Martin+LUT+SPP+AEPF+irfft | ~0.6 ms | ~2.7 ms |
| APF | bilinear IIR | ~50 µs | ~0.2 ms |

**Total chain CPU: ~10 ms per 10.7 ms block = ~93% CPU utilization on
a single thread.**  This is uncomfortably close to the audio drop
threshold.  The two Python loops (ANF, SQ) eat ~7.5 ms of that —
vectorizing them brings the total to ~3 ms (~28%) which is comfortable.

**FFT size upgrade impact** (NR2 FFT 256 → 1024, hop 128 → 256): 4
frames per block → 2 frames per block, but each frame is 4× more
bins.  Net: ~2× CPU for NR2.  From 0.6 ms to 1.2 ms.  Still well
within budget after the loop fixes.

**Memory:**
- NR1 min-stats ring: 562 frames × 129 bins × 4 bytes = 290 KB
- NR2 Martin trackers (~13 float64 × 129 + 8×129 ring): ~14 KB
- NR2 gain LUTs: 2 × 200 × 200 × 8 = 640 KB (both kept resident)
- Captured profile: 129 floats × 4 = 516 bytes per profile
- Per-profile JSON: ~3 KB

**FFT_SIZE 1024 changes:** all the per-bin arrays grow 4×, all the
ring/window buffers stay constant in time but grow in samples.  Total
memory bump: ~3 MB.  Negligible.

---

## 7. Prioritized Recommendations

**P0 — Do now (alongside or after v0.0.8 RX2):**

1. **Vectorize ANF and SQ inner loops.**  Half a day each.  Removes
   the CPU bottleneck.  Gates everything else.
2. **Add FFT-bin-resampling on profile load.**  Linear interp from
   saved bin axis to current bin axis.  Half a day.  Unblocks (3).
3. **Bump NR2 FFT_SIZE to 1024.**  With (2) in place, existing
   profiles transparently upgrade.  Re-validate AEPF, SPP,
   MINSTATS_BIAS tunings — likely 1 day of bench listening.  Highest
   perceptual win.
4. **Implement "Wiener-from-profile" as a new captured-profile
   pipeline.**  Bypass MMSE-LSA decision-directed updates when in
   captured-source mode.  2-3 days.  Fixes the mathematical mismatch
   and makes captured-profile mode genuinely better than live.

**P1 — Next (the differentiator stack):**

5. **Cyclostationary 60/120 Hz powerline modeling.**  1-2 weeks.
   Schema-bump captured profile to v2.  Lyra becomes the only ham
   SDR with this.
6. **Profile auto-suggest on band/mode change.**  1 day.
7. **Profile staleness toast notification.**  Half a day.
8. **Profile blending API + UI.**  1 day.
9. **Auto-toggle to captured source after a successful capture.**  2
   hours.

**P2 — Later (polish):**

10. **Spectral-peak-aware smart-guard.**  1 day.
11. **Per-band-segment profiles** (`freq_low_hz`/`freq_high_hz`
    metadata + window-aware auto-select).  1 day on top of (6).
12. **NR control reorganization** ("by-noise-type" Settings tab +
    visible Advanced expander on DSP+Audio panel).  1 day.
13. **WDSP SNB port** (spectral noise blanker, 862 lines C → ~400
    lines Python).  1 week.  Adds fundamentally new capability for
    non-impulse-domain bursts.

**P3 — Aspirational:**

14. **Adaptive profile refinement (slow drift)** with operator-set
    rate.  Half a day.
15. **Profile cross-correlation live readout.**  Half a day.
16. **Time-frequency 2D profile.**  1 week.  Lower priority than
    cyclostationary.

---

## 8. What NOT to Do

- **Don't chase RNNoise / DeepFilterNet integration.**  The user
  explicitly excluded NN frameworks.  Also: the captured-profile
  mode, properly implemented per (4) and (5) above, can match
  RNNoise on stationary HF noise with full operator transparency.
  NN models are black boxes with calibration mismatch on ham band
  noise.

- **Don't port WDSP `rnnr.c` (the GRU-based RNN reducer).**  Same
  NN-exclusion plus it depends on a pretrained model file that
  wasn't designed for HF audio.

- **Don't enlarge the captured-profile JSON to 4 KB per file by
  adding waveform thumbnails** — keep the data file lean; render
  thumbnails from `magnitudes` on-demand in the manager.

- **Don't try to make NR1 and NR2 share strength sliders.**  They're
  algorithmically different and have different sweet spots.  The
  current 0..1 (NR1) / 0..2 (NR2) is OK; just label the controls
  clearly.

- **Don't move SQ after NR.**  WDSP does, but Lyra's pre-NR placement
  is correct for the AM-broadcaster harmonics in N8SDR's actual
  environment.  Keep it.

- **Don't add an operator-configurable chain order.**  Sounds like a
  power-user feature, but most operators will misconfigure it and
  report bugs that are actually their own re-ordering.  If you do
  this, lock it behind a `LYRA_DSP_CHAIN_ORDER` env var (developer-
  only).

- **Don't bump `FFT_SIZE` to 4096 (matching WDSP exactly).**  4096 at
  48 kHz = 85 ms internal latency = audible on CW.  1024 is the
  right compromise.  (WDSP gets away with 4096 because they
  downsample EMNR to lower internal rates — Lyra's 48 kHz audio rate
  is hard.)

- **Don't unify NR1 and NR2 into a single processor with mode flag.**
  They share the STFT framing but diverge meaningfully (NR1 =
  magnitude subtraction; NR2 = log-spectral amplitude estimator).
  Code duplication here is justified.

---

## 9. Open Questions for the Operator

1. **AC mains frequency.**  Is N8SDR's site fed by 60 Hz or has
   there been variability?  (Cyclostationary modeling needs to be
   configurable per region.)

2. **Profile library size expectation.**  Will operators have 10
   profiles or 200?  Affects auto-select cost (linear scan is fine
   to ~50).

3. **Acceptable latency budget.**  Going to FFT=1024 adds 8 ms
   latency on NR.  CW operators using the chain at 250 Hz BW + LMS
   already accept ~10 ms — is +8 ms still OK?

4. **Captured-profile mode default after RX2 lands.**  Should RX2
   share RX1's captured profile, get its own, or capture them
   simultaneously?

5. **Mode-aware profiles.**  When operator captures on USB and
   switches to LSB, should the profile auto-disable, mirror across
   DC, or flag a warning?

6. **Smart-guard sensitivity.**  Three operator-tunable presets
   (Strict / Normal / Permissive) or stays as a single CV threshold?

7. **Cyclostationary modeling priority.**  Big effort; high payoff.
   Is the operator willing to invest 1-2 weeks of dev in this for
   the differentiator?  Or prefer P0/P1 polish first?

8. **Per-band auto-suggest UX.**  Toast notification ("Powerline 80m
   profile available") vs silent auto-load vs manager-only?  My
   recommendation is silent auto-load with a status badge in the
   captured-source indicator showing which profile is active.

---

**Audit complete.**  The deepest single technical insight is in
§2.2 / §4.2(c) — the captured profile + MMSE-LSA mismatch — which
has been quietly degrading captured-source NR2 mode since it shipped.
The biggest single feature differentiator is cyclostationary
powerline modeling per §4.3(c).  The biggest CPU win is vectorizing
the ANF + SQ loops per §6.
