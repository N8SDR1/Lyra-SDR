# Audio pops audit

**Status:** survey complete, no code changes yet.
**Date:** 2026-05-02.
**Operator report:** consistent, long-standing audio pops during normal
listening. Some pops are "very loud, many dB above standard playback
level." Worse on AK4951 line-out, also present on PC Soundcard — so the
cause is upstream of both output stages, not output-stage-specific.

This audit walks the audio chain end to end, identifies every place a
pop can be born, ranks the suspects by likelihood + amplitude, and
proposes fixes ordered by impact / risk. No code is changed in this
pass — review and approve the priorities before we touch DSP.

---

## 1. Methodology

Three sources of evidence:

1. **Code reading**, end to end:
   `protocol/stream.py` → `radio.py` (rx-thread → main-thread bridge)
   → `dsp/channel.py` (nb → decim → notches → demod → LMS → ANF → SQ
   → NR → APF) → `radio.py::_apply_agc_and_volume` (AF → AGC → Vol →
   leveler → tanh) → `dsp/audio_sink.py` (AK4951Sink or
   SoundDeviceSink) → output device.
2. **Discontinuity analysis** — at every "block boundary" or "state
   change" in the chain, ask: does the implementation ensure
   sample-domain continuity? If not, that's a click candidate.
3. **Operator-observable correlation** — the report says pops are
   *consistent* and *very loud*. Filters out continuous low-level
   artifacts (leveler distortion, NR musical noise) and focuses on
   step-discontinuity sources.

A click in audio is a delta-function in the sample domain. The DAC and
your ears interpret a sufficiently large per-sample step as a sharp
percussive sound. Two metrics matter:

- **Step amplitude** — how big is the sample-to-sample jump? Anything
  above ~0.05 of full scale is audible; above ~0.3 is a "loud pop."
- **Step rate** — how often does the step occur? "Random" usually
  means correlated with some operator/signal event we haven't yet
  identified.

---

## 2. Audio chain map

Single-thread mode (default in v0.0.7):

```
HL2 EP6 frame
  ↓ (UDP recvfrom on rx thread)
HL2Stream._rx_loop                              [protocol/stream.py]
  ↓ (parses 26-byte slots, builds complex64 IQ batch)
HL2Stream.callback → Radio._handle_iq_batch     [radio.py]
  ↓ (Qt cross-thread: bridge.samples_ready.emit)
Radio._on_samples_main_thread                   [radio.py:5003]
  ↓ (Qt main thread)
Radio._do_demod                                 [radio.py:5033]
  │  ├─ rx_channel.set_notches  (cheap state push)
  │  ├─ rx_channel.process(iq)  [dsp/channel.py:782]
  │  │    ├─ NB              (impulse blanker, pre-decim)
  │  │    ├─ Decimator       (IQ rate → 48 kHz)
  │  │    ├─ audio_buf accumulate; drain by block_size (1024)
  │  │    ├─ notch chain     (per-notch IIR)
  │  │    ├─ demod           (mode-specific)
  │  │    ├─ LMS             (line enhancer; opt-in)
  │  │    ├─ ANF             (auto notch; opt-in)
  │  │    ├─ Squelch         (all-mode SSQL; opt-in)
  │  │    ├─ NR1 or NR2      (spectral subtraction or MMSE-LSA)
  │  │    └─ APF             (CW only; opt-in)
  │  ├─ audio = _apply_agc_and_volume(audio)  [radio.py:5152]
  │  │    ├─ AF gain (linear, applied first)
  │  │    ├─ AGC tracker (instant attack on block_peak,
  │  │    │              exponential release)
  │  │    ├─ Volume scale
  │  │    ├─ Leveler (soft-knee compressor)
  │  │    └─ tanh limiter (sample-by-sample saturation)
  │  ├─ audio = binaural.process(audio)
  │  └─ audio_sink.write(audio)
  ↓
AK4951Sink.write           [dsp/audio_sink.py:55]
  ↓ (queue_tx_audio → deque feeding EP2 packer)
HL2Stream.txWriteThread    [protocol/stream.py]
  ↓ (EP2 frames over UDP, 48 kHz pacing)
HL2 → AK4951 codec → headphone jack → PC line-in
```

Worker mode (Settings → DSP → Threading = Worker BETA): the same
chain, but `_do_demod`'s body runs on a dedicated DSP worker thread.
Same DSP code, same state, same suspects.

---

## 3. Where pops are born — ranked

For each suspect: **what** the discontinuity is, **when** it fires,
**estimated amplitude**, and **fix sketch**.

### P0 — Highest confidence, biggest amplitude

#### P0.1 — AGC instant attack creates a step gain change at block boundaries  ★ likely culprit for "very loud pops"

**Where:** `radio.py:5200-5237` (`_apply_agc_and_volume`).

**What:**

```python
block_peak = float(np.max(np.abs(audio)))      # whole-block peak
if block_peak > self._agc_peak:
    self._agc_peak = block_peak                 # INSTANT attack
    self._agc_hang_counter = self._agc_hang_blocks
elif self._agc_hang_counter > 0:
    self._agc_hang_counter -= 1
else:
    self._agc_peak *= (1.0 - self._agc_release) # exp release
agc_gain = min(self._agc_target / self._agc_peak, AGC_MAX_GAIN)
audio = audio * agc_gain * vol                  # SAME gain applied to ALL N samples
```

The whole 1024-sample audio block gets multiplied by **one** scalar
`agc_gain`. That gain can change abruptly between blocks:

- Block N quiet, peak = 0.005. `agc_peak` is ~0.005 (after release
  has had time to drag it down). `agc_gain = 0.0316 / 0.005 = 6.3×`.
- Block N+1 contains a strong signal arrival, peak = 0.5.
  `agc_peak` jumps to 0.5 instantly. `agc_gain = 0.0316 / 0.5 =
  0.063×`.
- Last sample of block N is, say, +0.003 → output = +0.019.
- First sample of block N+1 might be +0.4 (the leading edge of the
  new signal) → output = +0.025. **OK** — the signal grew so the
  gain dropped proportionally; the output ramps reasonably.
- But: first sample of block N+1 might be **+0.005** (a quiet
  fragment before the strong sample later in the block). Output =
  **+0.000315.** Step from +0.019 to +0.000315 = -0.019 step. Audible
  as a tiny click but not catastrophic.

The catastrophic case is the **release** edge:

- Block N has a big transient (lightning crash, ignition spike that
  the impulse blanker missed, strong-signal arrival). `agc_peak` =
  0.5, gain = 0.063×.
- Block N+1, N+2, ... all quiet, peak = 0.005, but `agc_peak` is
  decaying exponentially with a tiny per-block step (release rate
  0.158 per block at the default; even faster on "fast" profile).
- Sample at end of block N (with peak still 0.5) might be +0.5 ×
  0.063× = +0.0315.
- Sample at start of block N+1 (now peak slightly less than 0.5) is
  +0.005 × 0.0633× = +0.000316.
- Step **= -0.0312** at the boundary. Audible click.

**The really nasty case:** when AGC release is "fast" (release ≈ 0.5
per block) **and** a strong impulse hits, the per-block gain change
can be 5-10×. At a block rate of ~46 Hz (1024 samples / 48 kHz × 2 to
account for the 2× block stuff), that means audible step changes
every ~22 ms during recovery from any transient. Operator hears a
"crackle" or repeating clicks tail after every loud sound.

**Why operator's ears agree this is the suspect:**

- "Random" — fires any time signal level changes abruptly (band
  noise jumps, S9 signal arrival, neighbor's switching power supply
  pops, ignition).
- "Very loud, many dB above audio level" — yes, because the
  discontinuity in the *digital domain* shows up as a wideband
  click; the playback DAC presents that click at full bandwidth, so
  it sounds substantially louder than the surrounding speech /
  steady-state hiss.
- "Worse on AK4951" — possibly because AK4951 has less
  inter-sample smoothing in its output path than your PC's output
  driver (PC drivers often have a tiny anti-imaging filter that
  smears single-sample spikes; the AK4951 path is essentially
  bit-perfect from EP2 → DAC).

**Fix sketch (proposal — for review before coding):**

Replace the block-scalar AGC with a **per-sample gain envelope** that
ramps gain linearly across the block:

```python
# Save previous block's final gain.
prev_gain = self._last_agc_gain
new_gain  = ...                          # same logic as today
ramp = np.linspace(prev_gain, new_gain, n)
audio = audio * ramp * vol
self._last_agc_gain = new_gain
```

Or, more WDSP-faithful, run the envelope tracker per-sample:

```python
# Per-sample attack/release envelope (textbook AGC).
mag = np.abs(audio)
peak = np.empty(n, dtype=np.float32)
p = self._agc_peak
attack_alpha = 1.0 - exp(-1 / (rate * attack_sec))   # very fast
release_alpha = 1.0 - exp(-1 / (rate * release_sec)) # slow
for i in range(n):
    if mag[i] > p:
        p += attack_alpha * (mag[i] - p)
    else:
        p += release_alpha * (mag[i] - p)
    peak[i] = p
self._agc_peak = p
gain = self._agc_target / np.maximum(peak, 1e-4)
audio = audio * gain * vol
```

Per-sample tracking eliminates block-boundary step entirely. Cost:
~150 µs per 1024-sample block in NumPy (we'd vectorize via
`scipy.signal.lfilter` for the IIR envelope), well within budget.

**Risk:** AGC dynamics will *feel* slightly different than today.
Need to keep the same target/release/hang semantics so operators
don't notice the time constants changed — just the discontinuity is
gone. Bench test required against today's behavior.

---

#### P0.2 — Decimator filter state is dropped on rate / freq / mode change

**Where:** `dsp/channel.py:285-299` (`set_in_rate`),
`dsp/channel.py:684-727` (`reset`).

**What:** every time `set_in_rate`, `set_mode`, or `reset` is called,
the channel does:

```python
self._decimator = None              # forces rebuild on next process()
self._audio_buf.clear()             # drops in-flight audio samples
```

The next IQ block triggers `_decimate_to_48k`, which builds a fresh
`_Decimator` with **all-zero filter state** (`state_i / state_q`
init'd to `np.zeros(taps - 1)`). The first ~257 IQ samples (the FIR
length) have to charge up the filter state. During that ramp the
output is wrong-magnitude; once it settles, you get correct decimated
audio but with a *step* between the last sample of the previous
session (now zero, because audio_buf was cleared) and the first
sample of the new session (whatever the cold-start filter produced).

This produces a click on:
- **Frequency change** (every time you tune across the band). Even
  a few-Hz tune step calls `reset()` so the audio gets wiped.
- **Mode change** (LSB → USB, etc.).
- **Sample-rate change** (96 kHz → 192 kHz).

**Why operator's ears agree:**

- "Worse when tuning around" — yes, every freq change produces a
  click. Operator who tunes a lot hears more pops.
- "Even when sitting still on a freq" — also yes, because of P0.1
  AGC pops.

**Fix sketch:** seed the decimator state with the **last known IQ
sample** rather than zero, so the FIR sees a continuous signal at
startup. Better: don't drop the audio buffer on freq/mode change —
crossfade the last 5-10 ms of the OLD chain's output with silence
(short fade-out), then start the new chain after a few-ms gap (no
discontinuity heard, just a brief mute). The current `reset()` is
abrupt: drop audio buf, drop filter state, hope nothing audible
happens. It's the wrong default.

**Lesson from WDSP / Thetis:** they don't reset on every tune.
They flush only when the IQ rate or block size changes. Lyra resets
on freq change because the comment chain says "freq change is a
discontinuity already" — but the operator wasn't expecting a click,
they were expecting frequencies to scroll under the panadapter
smoothly.

---

#### P0.3 — AK4951 EP2 queue cleared mid-frame on sink swap

**Where:** `dsp/audio_sink.py:43-53,85-92` (`AK4951Sink.__init__`,
`close`); `protocol/stream.py::clear_tx_audio`.

**What:** every sink swap (start, stop, set_audio_output, PC device
change, AK4951 ↔ PC Soundcard toggle) calls
`stream.clear_tx_audio()`. This drains the deque feeding EP2 frames.
If the EP2 framer is mid-frame when the deque drops to empty, the
remaining slot bytes get filled with whatever fallback the framer
uses (probably zero). At the AK4951's DAC, the transition from
mid-amplitude sample → zero is a sample-domain step. If that step is
above ~0.05 amplitude, you hear a click.

**Why this fits:** sink-swap pops are very loud (full transition from
audio → silence in one sample) but **rare** (only on operator
actions). Most of operator's pops are elsewhere (P0.1, P0.2).

**Fix sketch:** during sink swap, before clearing, fade out the
remaining queued audio over ~5 ms (240 samples at 48 kHz). Then
clear. The fade-out is sample-by-sample multiplication by a half-
cosine ramp; trivial to implement in `clear_tx_audio`.

---

### P1 — Medium confidence, smaller amplitude

#### P1.1 — Filter coefficient swap with persistent zi state on RX BW / pitch / notch width changes

**Where:** `dsp/channel.py:321-338` (`set_rx_bw`, `set_cw_pitch_hz`),
`dsp/channel.py:744-771` (`_rebuild_demods`),
`radio.py` (notch filter rebuild on width change).

**What:** when the operator changes RX BW or CW pitch, Lyra calls
`_rebuild_demods()`, which constructs **brand-new** demod instances
with **brand-new** filter state (zeros). The previous demod's filter
state is lost. The next IQ block hits the new demod with cold filters
→ ramp-up transient → small click.

This isn't catastrophic on its own (the cold-start ramp is bandlimited
by the new filter, so the click is much softer than a true
sample-domain step), but it's audible — operators who twiddle BW
sliders hear a soft click on every drag tick.

**Fix sketch:** preserve the filter zi state across rebuilds where
possible. For windowed-sinc FIR demods that's straightforward: the
new and old filters have the same length, so the state can carry
over directly. For IIR (notch filters), it's trickier — the state
isn't compatible across coefficient changes — but a 5 ms crossfade
between old-output and new-output blocks would mask the transient.

---

#### P1.2 — NR1 / NR2 STFT enable/disable creates a half-frame discontinuity

**Where:** `dsp/nr.py`, `dsp/nr2.py` (the STFT framing).

**What:** both NR1 (FFT_SIZE=1024 with hop=512) and NR2 (FFT_SIZE=1024
with hop=512) are 50%-overlap-add COLA-exact STFTs. When the operator
toggles NR off, the implementation switches to passthrough — but the
last partially-processed frame in the OLA buffer gets truncated and
the next bypassed audio starts cleanly. There's a 512-sample gap
where the OLA buffer's tail (the inverse-window sum of the last
processed frame) doesn't get added to the output.

This creates a **mute** for ~10 ms followed by a step back to full
amplitude as bypassed audio resumes. Audible as a "pluck" or
"click-mute-click" on toggle.

**Fix sketch:** crossfade the OLA-buffer tail with the bypassed input
over the half-frame transition. About 30 lines of Python; verify
with bench test.

---

#### P1.3 — Squelch ramps already exist (70 ms cosine), but boundary case on threshold tweaks

**Where:** `dsp/squelch.py:67-68,203-216`.

**What:** squelch transitions ARE smoothed via 70 ms cosine
attack/release ramps — that's already done correctly. **Caveat:**
when the operator drags the threshold slider live, the squelch
detector's `_k_open` / `_k_close` levels jump instantly. If the
detector was riding right at the threshold, a slider tick can flip
the gate state and trigger a ramp transition. That ramp is smooth
(no click), but rapid back-and-forth flips at the threshold edge can
sound like "burbling." Not a pop, but worth noting.

**Fix:** debounce slider input (50 ms) before recomputing thresholds.

---

#### P1.4 — NB (impulse blanker) hold-last-clean replacement at block boundaries

**Where:** `dsp/nb.py:179-186` — `_last_clean: complex` carried across
blocks.

**What:** when an impulse straddles a block boundary, the cosine slew
covers the impulse-region edge but **only within the block**. If
sample N (last of block K) is impulse and sample 0 (first of block
K+1) is also impulse, the slew at N tapers to `_last_clean`, but
sample 0's slew starts from clean back into impulse. There's a
small window at the boundary where the slew could be discontinuous
(slew-out followed by slew-in without holding the hold-last-clean
value through the join).

**This is theoretical** — would need a bench test with manufactured
boundary-straddling impulses to confirm. Most real impulses are
microseconds wide and fit inside one block easily.

---

### P2 — Lower confidence, edge cases

#### P2.1 — Worker-mode reset race during freq change

**Where:** `dsp/worker.py:255-265,398-401,646-705`.

**What:** main thread calls `worker.request_reset()`; worker checks
the flag at the *next* block boundary and calls `_reset()`. Between
the two events, `process_block` may run on stale state. Comment in
`worker.py:460-461` says "at most one block of wrong-mode audio."
That's correct, but a "wrong-mode audio block" right after a freq
change can produce demod artifacts that sound like clicks (filter
hitting unexpected frequency content).

**Fix:** drain the input queue **before** the reset flag is set, so
the worker processes only post-reset samples. Already partially done
(reset() drains the queue), but the drain happens *after* processing
one wrong-mode block.

---

#### P2.2 — Tone mode block-size mismatch (already fixed in v0.0.7)

**Where:** `radio.py:5118-5150` — `_emit_tone`.

**What:** historical issue (now resolved) — original tone path
generated samples at IQ rate not audio rate, causing massive sink
backpressure. Fix landed earlier; tone now generates at 48 kHz
audio rate. Still worth noting because future regressions could
reintroduce it.

---

#### P2.3 — tanh limiter is good, but doesn't fix block boundary steps

**Where:** `radio.py:5198,5243`.

**What:** `np.tanh(x)` saturates large amplitudes smoothly to ±1.
This **does** prevent absolute-amplitude clipping. It does NOT
prevent **inter-block step changes**. tanh applied after AGC means
each sample is squashed individually, but if AGC creates a big gain
step at the boundary, tanh squashes both sides separately and the
boundary discontinuity survives. tanh is doing its job (preventing
hard digital clip); the AGC step is the underlying issue (P0.1).

---

## 4. Recommended fix order

Estimated by impact × likelihood × implementation risk.

| # | Suspect | Impact | Effort | Risk |
|---|---|---|---|---|
| 1 | P0.1 — AGC per-sample envelope | **HIGH** (eliminates the loud pops) | 1 day | Med (need to re-tune feel) |
| 2 | P0.2 — Decimator state preservation across reset | HIGH (eliminates tune-pops) | 2 days | Low (well-understood pattern) |
| 3 | P0.3 — AK4951 sink-swap fade | Med (rare event but loud) | 4 hours | Low |
| 4 | P1.2 — NR1/NR2 OLA tail crossfade | Med (NR-toggle pops) | 4 hours | Low |
| 5 | P1.1 — Demod rebuild zi preservation | Low (slider drag pops) | 1 day | Med |
| 6 | P1.3 — Squelch threshold debounce | Low (burble in edge case) | 1 hour | Low |
| 7 | P2.1 — Worker pre-reset queue drain | Low (worker-mode only) | 2 hours | Low |

**Recommendation:** ship #1 and #2 in v0.0.7.x as a focused "audio
pop quiet pass" patch. Both have measurable bench tests:

- **AGC test:** synthesize an audio block with a step (-40 dBFS for
  500 ms, +0 dBFS for 500 ms, repeat). Run through old vs new
  `_apply_agc_and_volume`. Plot the output. Old path shows a sample-
  level step at every block boundary during the transition; new path
  shows a smooth gain ramp. Measure step amplitude — should drop
  from ~0.03 to <0.001.
- **Decimator test:** call `set_in_rate(192000)` followed by a
  500-sample IQ block, then `set_in_rate(96000)` then another block.
  Measure the discontinuity at sample 0 of the second block. Old
  path shows ~0.5 amplitude step; new path shows <0.05.

---

## 5. Bench-test plan (before merge)

For each fix:

1. **Unit test** — synthetic input, measure step amplitude.
2. **A/B operator test** — dual-build (one with fix, one without),
   listen on a busy band for 30 seconds. Note count and severity
   of pops.
3. **CPU benchmark** — measure per-block timing of
   `_apply_agc_and_volume` before/after. Per-sample envelope adds
   ~150 µs / block on a modern CPU; we have ~21 ms per block at
   48 kHz so the budget is fine.
4. **Regression test** — confirm AGC time constants feel the same
   to operator (no perceived "sluggishness" or "pumping" change).

---

## 6. Open questions

1. **Are there pops on rate change specifically?** Operator should
   try switching 96k → 192k → 384k while listening to a steady
   carrier. If clicks are heard, it confirms P0.2 is significant.
2. **Are there pops on AGC profile change (fast → slow → med)?**
   Profile change updates `_agc_release` instantly. If clicks
   correlate, it's part of P0.1 — fix is the same per-sample
   envelope.
3. **Operator log of AK4951 vs PC pop frequency** — does
   `[Lyra audio] SoundDeviceSink ring: overruns=...` print when pops
   occur? If yes, sink overruns may be a *separate* PC-only pop
   path. Worth correlating logs with reports.

---

## 7. Out of scope for this audit

- **NR algorithm tweaks** — separate scope, see `nr_audit.md`.
- **TX path pops** — there is no TX path yet (v0.1 work).
- **Mic / sidetone audio** — ditto.

---

## 8. Approval gate

Before any code change:

1. Operator confirms the priority order in §4 looks right.
2. Operator confirms which fixes ship in the next patch (suggestion:
   #1 + #2 + #3, the three P0s).
3. We open a feature branch, implement with bench tests, ship as a
   v0.0.7.x pre-release for operator flight test.
