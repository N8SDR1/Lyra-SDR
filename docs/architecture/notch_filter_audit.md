# Manual notch filter audit

**Status:** survey complete, no code changes yet.
**Date:** 2026-05-02.
**Operator report:** "Manual notches just don't seem to work as well
as they could." Pinpoints the manual notch implementation, not ANF
(which was audited as part of the NR stack).

This audit reviews Lyra's `NotchFilter` implementation in
`dsp/demod.py:189-271`, measures its theoretical performance against
operator expectations, and proposes specific upgrades. Like the audio
pops audit, no code changes here — review the priorities first.

---

## 1. Methodology

1. **Code review** of `NotchFilter` and how it's used by
   `Radio.add_notch / remove_notch / update_notch` and the channel's
   notch chain in `dsp/channel.py:824-828`.
2. **Theoretical performance analysis** — compute the actual transfer
   function depth, transition bandwidth, and stopband behavior at the
   designed Q values for typical operator settings.
3. **Comparison to reference implementations** — WDSP (`anf.c`,
   biquad cascades), commercial SDR clients (parametric notch
   sections), and the literature standard (bilinear-z notch with
   conjugate zeros on the unit circle).
4. **Operator-experience mapping** — what does the operator
   *perceive* when a notch "doesn't work as well as it could"?

---

## 2. Current implementation

### 2.1 Architecture

```
Notch (dataclass: abs_freq_hz, width_hz, active, deep, filter)
  ↓
NotchFilter (one biquad in IIR form, applied to I and Q separately)
  ↓ (operates on baseband IQ, in the channel chain BEFORE demod)
```

Two filter modes:

- **Off-DC notch (default):** scipy's `iirnotch(w0, Q)` — produces a
  2-pole 2-zero IIR section with conjugate zeros on the unit circle
  near `w0` and conjugate poles slightly inside.
- **DC blocker:** scipy's `butter(4, corner, btype='high')` — 4th-
  order Butterworth high-pass. Used when the operator clicks
  at/near the VFO marker; the iirnotch design degenerates at DC.

The `deep=True` flag cascades the SAME biquad twice in series with
independent state, doubling the dB attenuation at every offset.

### 2.2 The `iirnotch` design

`scipy.signal.iirnotch(w0, Q)` produces:

```
H(z) = b0 + b1·z⁻¹ + b2·z⁻²
       ─────────────────────
       1 + a1·z⁻¹ + a2·z⁻²
```

with zeros at `e^(±j·w0)` (perfectly on the unit circle — infinite
attenuation at exactly `w0`) and poles at `r·e^(±j·w0)` where
`r = (1 - α) / (1 + α)` and `α = tan(BW/2)`.

The **theoretical** notch is infinitely deep at the exact center
frequency. **In practice:**

- Float32 quantization of the coefficients limits the actual depth
  to ~80-100 dB at `w0` (well below audibility, fine).
- Bandwidth = -3 dB BW = `freq / Q` per scipy's parameter convention.
  Lyra uses `Q = max(freq_hz / max(width_hz, 0.5), 0.5)`.

**The problem is the shape outside `w0`:**

With Q=20 (a 7250 / 350 Hz center / width carrier), the off-`w0`
attenuation drops from "infinite" at `w0` to:

| Offset from w0 | Single-stage attenuation |
|---|---|
| 0 Hz | ~80 dB (float32 limit) |
| ±BW/4 ≈ ±90 Hz | ~6 dB |
| ±BW/2 ≈ ±175 Hz | -3 dB (the design point) |
| ±BW ≈ ±350 Hz | -1.5 dB |
| ±2·BW ≈ ±700 Hz | -0.5 dB |

Operator places a 350 Hz wide notch at 7.250 MHz to kill an AM
broadcast carrier. The notch *visually* spans 175 Hz on either side
of the carrier. But:

- A signal +20 Hz off the carrier is attenuated only ~3 dB —
  audible bleed-through. Operator hears "the carrier is killed but
  there's still a wash of broadcast audio coming through the
  notch."
- Adjacent-channel modulation (the AM sidebands at ±5 kHz from the
  carrier) is essentially untouched. Operator who placed the notch
  thinking "this kills the broadcast" finds the broadcast audio
  still audible because the modulation isn't on the carrier — it's
  in the sidebands.

**Deep mode** (cascade ×2):

| Offset from w0 | Deep (×2) attenuation |
|---|---|
| 0 Hz | ~80 dB (limit) |
| ±BW/4 ≈ ±90 Hz | ~12 dB |
| ±BW/2 ≈ ±175 Hz | -6 dB |
| ±BW ≈ ±350 Hz | -3 dB |
| ±2·BW ≈ ±700 Hz | -1 dB |

Better, but still leaves a "soft" notch shape. The operator wants a
**brick-wall stopband** within the visible notch region and **flat
unity** outside — the iirnotch approach gives a slow taper instead.

### 2.3 The DC-blocker mode

Used when operator clicks within ~width/2 of the VFO carrier:

```python
corner = max(width_hz * 0.5, 5.0)
self.b, self.a = butter(4, corner, btype='high', fs=rate)
```

This is **not really a notch** — it's a high-pass that kills DC and
everything below `width/2`. Asymmetric: the signal below the corner
is gone entirely; the signal above the corner passes. For the WWV-
on-carrier use case (operator wants to kill a constant carrier
exactly at VFO), it works adequately, but visually the notch
"icon" on the spectrum doesn't match the actual filter behavior.

---

## 3. What the operator wants vs what the filter delivers

| Operator mental model | Current filter behavior | Match? |
|---|---|---|
| "Kill 100 Hz wide chunk centered on this carrier." | -3 dB at edges of the 100 Hz region; -infinity only at the exact center. | ❌ — the visible kill region leaks 3 dB across most of its width. |
| "Stack two notches to kill two carriers — they don't interact." | Stacking IIR notches in series is fine in steady state; transient ringing on each can compound. | ✅ in steady state, ⚠ during settling. |
| "Make the notch deeper without making it wider." | Currently you can't — `deep=True` doubles depth AND tightens BW slightly (~30%). Operator can't independently control depth. | ❌ — depth is bound to width. |
| "The notch ring-tail is short." | Single biquad with Q=20: settling time ≈ 5 / (Q × BW) seconds ≈ 5 / (20 × 350) ≈ 0.7 ms. Cascaded biquad ≈ 1.5 ms. | ✅ — settling is fast. |
| "Drag-tune a notch and the audio is artifact-free." | Coefficient swap on every drag tick; state persists but the new coefficients applied to old state cause a transient. | ⚠ — soft click on every drag tick. |
| "Notch the broadcast carrier and its modulation goes away." | Modulation is in sidebands, not on the carrier — a narrow notch doesn't help. Operator confused. | ❌ — UX issue, not filter issue. The filter is doing what it's designed to do; the operator wants a wider notch (or AM broadcaster filter). |

The operator's "doesn't work as well as it could" maps mostly to the
first row: **the notch isn't as deep or as flat-floored as expected
within the visible kill region.** That's a design choice in the
filter, not a bug.

---

## 4. Reference implementations

### 4.1 WDSP

WDSP's `anf.c` is the auto-notch (LMS adaptive — already in Lyra as
ANF). For **manual** notches, WDSP uses cascaded biquad stages with
parametric Q + depth. Each stage is a peaking-EQ section with the
gain set to a deep negative value:

```c
// WDSP "notch" (peaking EQ with negative gain)
A = 10^(gain_db / 40)
alpha = sin(w0) / (2 * Q)
b0 = 1 + alpha*A;  b1 = -2*cos(w0);  b2 = 1 - alpha*A
a0 = 1 + alpha/A;  a1 = -2*cos(w0);  a2 = 1 - alpha/A
```

With `gain_db = -60` and `Q = 5`, this produces a notch with
**uniform -60 dB depth** over a tunable width and steep transition
shoulders. The depth is independent of the width.

### 4.2 Commercial SDR clients

SDR-Console, SDRuno, and Thetis all use **parametric notch** sections
(biquad-based) with three operator controls:

- **Frequency** — center
- **Width** (or Q) — bandwidth
- **Depth** — separate from width (typically -20 to -80 dB selectable)

Operators can dial in "narrow but deep" or "wide and gentle" without
changing the other dimension.

### 4.3 The ideal filter

For a true brick-wall notch, you'd use an FIR with windowed-sinc
design. Tradeoffs:

- ✅ Arbitrarily deep stopband (limited only by tap count)
- ✅ Flat passband
- ✅ Linear phase (zero phase distortion)
- ✅ No transient ringing on coefficient swap (state-only)
- ❌ Higher CPU cost (~256 taps for a 100 Hz notch at 48 kHz)
- ❌ Higher latency (256 / 48000 = 5.3 ms)

Probably overkill for SDR notches; biquad cascade with
parametric Q + depth is the sweet spot.

---

## 5. Recommended improvements

Ranked by impact / effort.

### N1 — Add a `depth_db` parameter (independent of width) ★ biggest UX win

Replace iirnotch with a peaking-EQ biquad parameterized by **width**,
**depth**, and **center**. Operator gets three knobs instead of two,
and the notch behaves the way SDR-client operators expect.

**Implementation:**

```python
def _design_parametric_notch(rate, freq_hz, width_hz, depth_db):
    A = 10 ** (depth_db / 40.0)               # depth_db is negative
    w0 = 2 * np.pi * freq_hz / rate
    Q = freq_hz / width_hz
    alpha = np.sin(w0) / (2 * Q)
    b = np.array([1 + alpha*A, -2*np.cos(w0), 1 - alpha*A])
    a = np.array([1 + alpha/A, -2*np.cos(w0), 1 - alpha/A])
    return b / a[0], a / a[0]
```

Default depth: -50 dB (operator-perceptible "kill", well below most
audio noise floors). Depth slider in Settings → Notches: -20 to -80
dB.

UX: keep the existing `width_hz` slider; add a `depth_db` slider.
Existing notches keep working — load saved ones with default depth
-50 dB.

### N2 — Cascade depth instead of "deep flag"

Replace `deep: bool` with an integer `cascade: int` (1, 2, 3, 4).
Each stage adds another biquad to the chain. With N1 in place:

- 1 stage at -50 dB = -50 dB notch at center, 6 dB shoulders within
  width.
- 2 stages at -25 dB each = -50 dB total at center, 3 dB shoulders.
  Sharper.
- 4 stages at -12.5 dB each = -50 dB total, 1.5 dB shoulders.
  Sharper still.

Operator picks the trade-off: shallow many = sharp, deep few = blunt.
Default = 1 stage. Costs CPU proportionally.

### N3 — Crossfade on coefficient swap (eliminate drag-tick clicks)

When the operator drags the notch frequency or width slider, do
this:

```python
# Compute new coefficients for the target setting.
new_b, new_a = design(new_freq, new_width, depth)
# Process the next 5 ms (240 samples at 48k) through BOTH old and new
# filters with their own state. Crossfade output linearly.
out_old = old_filter.process(samples[:240])
out_new = new_filter.process(samples[:240])
ramp = np.linspace(1.0, 0.0, 240)
out = out_old * ramp + out_new * (1 - ramp)
# After the crossfade, swap to new filter only.
old_filter = new_filter
```

Eliminates the "soft click on every drag tick" issue. Same pattern
as proposed for the AGC fix in audio_pops_audit.md (P1.1).

### N4 — DC-blocker mode visualization

The DC-blocker IS NOT a notch — it's a high-pass. Either:

- **Option A** — re-implement the on-carrier case as a true notch
  (very high Q biquad with the iirnotch design) and accept the
  near-DC quirks.
- **Option B** — render a DC-blocker on the spectrum with a
  different shape (translucent triangle below the frequency, not a
  symmetric pill). UX hint to the operator that this notch is a
  high-pass.

I'd lean **B** — the implementation is correct, just label it
honestly in the UI.

### N5 — Optional: FIR notch for stubborn carriers

For operators who want absolute brick-wall behavior, expose an FIR
notch as a per-notch option. 256-tap windowed-sinc design, latency
~5.3 ms, depth >100 dB. Use case: "I MUST kill this RF beacon, the
biquad cascade isn't deep enough." Niche, but the option is worth
having.

---

## 6. Recommended order

| # | Change | Impact | Effort | Risk |
|---|---|---|---|---|
| 1 | N1 — depth_db parameter | **HIGH** (operator gets the knob they expected) | 1 day | Low (drop-in design replacement) |
| 2 | N3 — crossfade on coefficient swap | Med (kills drag-pop) | 4 hours | Low |
| 3 | N2 — cascade integer | Med (operator can dial in sharper notches) | 4 hours | Low |
| 4 | N4 — DC-blocker visualization | Low (just clearer UX) | 1 hour | None |
| 5 | N5 — FIR notch option | Low (niche use case) | 2 days | Med (UI surface area) |

**Recommendation:** ship N1+N2+N3 as one "notch v2" feature. Operator
gets the depth knob, cascading, and click-free dragging in one go.
N4 ships alongside as a UI polish; N5 deferred unless operators
request it.

---

## 7. Bench-test plan

### Frequency response measurement

Generate a 20 Hz - 24 kHz log-swept sine, run through:
- Old single iirnotch at 1 kHz / 100 Hz width
- New parametric biquad at 1 kHz / 100 Hz / -50 dB depth
- New cascaded ×2 at 1 kHz / 100 Hz / -50 dB depth
- New cascaded ×4 at 1 kHz / 100 Hz / -50 dB depth

Plot magnitude response. Expected:
- Old: ~6 dB shoulder at ±25 Hz, ~3 dB at ±50 Hz.
- New ×1: ~50 dB at center, ~6 dB shoulder at ±25 Hz.
- New ×2: ~50 dB at center, ~3 dB shoulder at ±25 Hz.
- New ×4: ~50 dB at center, ~1.5 dB shoulder at ±25 Hz.

### Stack interaction test

Place 3 notches at 700 Hz, 1000 Hz, 1300 Hz on a swept sine. Verify
each notch's stopband is independent and the in-between frequencies
pass at unity within tolerance (<0.5 dB). Both old and new should
pass; new should be sharper.

### Drag-tick click test

Programmatically tick the notch frequency by 1 Hz / 50 ms for 10
seconds. Capture audio output. Old: audible click on every tick.
New (with N3): silent.

### CPU benchmark

Measure per-block timing of channel.process with:
- 0 notches
- 4 notches (single stage each)
- 4 notches (×2 cascade each)
- 4 notches (×4 cascade each)

Per-stage cost should be ~50 µs at 1024 samples; 16 stages would be
~800 µs / block, well within the ~21 ms block budget.

---

## 8. Out of scope for this audit

- **ANF (auto notch)** — already audited as part of NR; that audit
  found ANF performance acceptable. No changes proposed.
- **Notch placement UX** (snap-to-peak, click-and-drag interactions
  on the spectrum) — covered separately in
  `click_to_tune_plan.md`.

---

## 9. Approval gate

Before any code change:

1. Operator confirms the priority order in §6.
2. Operator confirms the proposed UX (separate width / depth
   sliders, cascade integer instead of `deep` boolean).
3. We open a feature branch and implement N1+N2+N3 with bench
   tests, ship as v0.0.7.x or v0.0.8 alpha.

Existing Notch dataclass migration: `width_hz` and `active`/`deep`
already persist in QSettings. Migration:

```python
# Old: deep=True → cascade=2, depth_db=-50 (preserve perceived depth)
# Old: deep=False → cascade=1, depth_db=-30 (preserve perceived shallow)
```

Settings load translates the old fields to new on first read, then
saves the new schema.
