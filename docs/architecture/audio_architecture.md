# Lyra Audio Architecture

**Status:** Locked 2026-05-06 after operator-driven Thetis source trace + Thetis settings database analysis.
**Authority:** This document supersedes earlier scattered audio discussions in CHANGELOG entries v0.0.7.x through v0.0.9.5.
**Related:** `CLAUDE.md` §13 (operative summary), `CLAUDE.md` §6.7 (hardware capability struct).

---

## 1. The problem we kept failing to solve

From v0.0.7.x through v0.0.9.5, operators reported intermittent audio
glitches on Lyra's "PC Soundcard" output mode — pops, clicks, brief
audio dropouts. The pattern in the console diagnostic showed
alternating phases:

```
overruns=27 underruns=0   (DSP outpacing device)
overruns=22 underruns=0   (still overrunning)
...
overruns=0  underruns=16  (now device outpacing DSP)
overruns=0  underruns=11
```

That alternating behavior is the diagnostic signature of **two
unsynchronized crystal oscillators** both nominally running at
"48 kHz." The HL2 has its own crystal. The PC sound card has its
own crystal. They differ by parts-per-million within their tolerance
(typically ±50 ppm), and over time the ring buffer between them
either fills (overrun → discard samples → click) or drains
(underrun → silence gap → click).

Multiple deep dives chased this through producer-side fixes:
threading priority, MMCSS Pro Audio class, GIL switchinterval,
EP2 cadence rebuild, semaphore-driven write timing. None of those
addressed the consumer side. The consumer side is where the fix
lives.

## 2. What Thetis actually does

Operator-supplied Thetis settings database
(`C:\Users\N8SDR\Downloads\for claude.xml`) and a focused trace of
the Thetis source tree (Thetis 2.10.3.13) produced concrete answers.

### 2.1 Thetis's two audio paths

Thetis has exactly two audio output paths for HL2/HermesLite mode:

**Path A — HERMES codec (default, ~95% of HL2 users).**
`audioCodecId = HERMES` in `cmsetup.c:75`. Audio goes:

```
WDSP RXA  →  fexchange0()  →  cmaster::aamix()  →  network.c::SendpOutboundRx()
        →  networkproto1.c packs L/R into EP2 frames  →  HL2 over UDP
        →  HL2 onboard codec  →  headphone jack
```

Single crystal (the HL2's). No clock drift. No resampler needed.
This is what your Thetis settings DB shows you running:
`last_radio_hardware = HermesLite`, `chkHL2IOBoardPresent = False`,
no `comboMainAudioOutput` or equivalent setting (the absence is
the proof — Thetis HermesLite mode hard-wires audio to the
HERMES path).

**Path B — ASIO low-latency PC audio.**
`cmasio.c:90-91` creates `rmatchV` resamplers for ASIO output.

```
WDSP RXA  →  cmasio.c::xrmatchOUT()  →  ASIO callback  →  PC sound card
```

This DOES have clock drift (HL2 vs sound card crystals), and Thetis
solves it via the WDSP `rmatch.c` adaptive resampler (see §3 below).

### 2.2 What Thetis explicitly does NOT do

WASAPI output is **not implemented** in Thetis. From
`netInterface.c:1757-1759`:

```c
case WASAPI: // not implemented
```

Thetis users on PC sound card use ASIO. WASAPI was never the
production path. The "two-clock drift on WASAPI" problem is a
Lyra-only problem because Lyra is using a path Thetis avoided.

### 2.3 VAC vs primary audio

The Thetis DB shows `chkAudioEnableVAC = True` with VAC1 routed
through MME driver to Virtual Audio Cable's "Line 1" / "Line 2"
endpoints. **VAC is for digital modes** — it carries audio to a
virtual cable so WSJT-X / FT8 / fldigi listen there. VAC is not
the operator's "speaker" output. The operator's speaker output in
HermesLite mode is always the HERMES path.

### 2.4 Thetis runs BOTH sinks simultaneously — Lyra deliberately does NOT (Amendment A4, Round 3 2026-05-11)

A subtlety worth pinning down before v0.1 RX2 work begins: in
Thetis's HermesLite configuration, the HERMES codec path
(Path A above) AND the IVAC virtual-audio-cable / ASIO path
(Path B) can both run **at the same time** — the operator's
ears get the HERMES jack output, while digital-mode software
(WSJT-X / FT8 / fldigi) gets a parallel feed via IVAC.  This is
why Thetis exposes both `audioCodecId = HERMES` for the codec
and a complete IVAC config block for the virtual cable.

**Lyra deliberately makes these mutually exclusive.**  The
"Out" combo on the DSP+Audio panel is a single selector — HL2
audio jack OR PC Soundcard, not both.  The reasoning, all
operator-decision-grade:

1. **Simpler operator mental model.**  "Which sink is hearing
   audio right now?" has a single answer at any moment.  Ham
   operators who switch between modes don't need a sub-config
   panel deciding which path each consumer reads from.
2. **No double-resampler CPU cost.**  Path B's `rmatch+varsamp`
   PI loop runs only when the operator has actually selected it.
   On Path A operators (~95% of HL2 users), the entire `rmatch`
   stage is idle.
3. **Digital-mode users use TCI instead of audio loopback.**
   Lyra has first-class TCI support (see `docs/help/tci.md`);
   SDRLogger+, JTDX, and N1MM+ consume audio through TCI's
   audio-over-network protocol rather than via VAC.  TCI is
   sample-accurate, latency-known, and doesn't require a
   virtual-audio-cable driver install.  The dual-sink Thetis
   pattern was a workaround for a problem TCI solves cleanly.
4. **AAmixer routing matrix simpler on the destination axis.**
   With one active sink, route → sink is trivial.  With two
   parallel sinks, every route has to know "to which sink."
   **IM-3 Round 1 2026-05-11 correction** to the earlier draft:
   the state-machine *source* axis is NOT simplified by single
   sink — it remains 8-way determined by (Power × MOX × diversity
   × PS), regardless of sink count.  Most cases collapse on
   no-power-no-MOX-no-PS, but the full enumeration (per Thetis
   console.cs:28259-28333 HermesLite path) is:
   ```
   (a) Power off               → all streams muted
   (b) Power on, !MOX, !div, !PS → RX1 + RX1S + RX2EN + MON active
   (c) Power on, !MOX, !div, PS  → identical to (b); PS-armed
                                     without MOX changes nothing
   (d) Power on, !MOX, div, !PS  → RX1 + RX1S + MON (no RX2)
   (e) Power on, !MOX, div, PS   → identical to (d)
   (f) Power on, MOX, !div, !PS  → RX1 + RX1S + RX2EN + MON
                                     (operator may hear own TX
                                      sidetone gated by per-RX
                                      MuteRX1OnVFOBTX/MuteRX2OnVFOATX)
   (g) Power on, MOX, !div, PS   → MON only (RX silenced for PS calibrate)
   (h) Power on, MOX, div, PS    → MON only (same)
   ```
   Single-sink design simplifies the *destination* axis only.
   Operator-mute toggles (`MuteRX1OnVFOBTX`,
   `MuteRX2OnVFOATX`) live as **post-mixer per-stream
   multipliers**, NOT state-machine axes (otherwise the matrix
   would explode 4× more).  Phase 0 stubs the diversity axis
   to 0 (no diversity in v0.1); v0.2 activates the MOX/MON
   cases; v0.3 activates the PS-disable-RX rule.

**v0.4 ANAN impact:** ANAN family has no onboard codec, so
"HL2 audio jack" is unavailable.  PC Soundcard becomes the
only option there — no behavior change to the mutual-exclusion
contract.  When digital-mode operators ask "how do I run
WSJT-X on Lyra + ANAN simultaneously," the answer remains
TCI, not dual sinks.

**Operator-override escape hatch:** if a future tester reports
a legitimate need for parallel sinks (e.g. running the HL2
jack for the operator's headphones AND piping audio to a
recorder app that can't speak TCI), the AudioMixer abstraction
in `lyra/dsp/mix.py` supports it — add a second route, second
sink object.  But this stays out of v0.1-v0.3 by design.
Document the architectural choice; don't paint into a corner.

## 3. WDSP's `rmatch` adaptive resampler

Used by both VAC and ASIO paths. Built from two layers:

### 3.1 `varsamp.c` — variable-ratio polyphase resampler

`xvarsamp(VARSAMP a, double var)` at `varsamp.c:126-181`. Per-call
`var` parameter multiplies the nominal rate ratio. With
`varmode=1`, linearly interpolates the inverse rate from old to
new across the buffer (`varsamp.c:135-139, 150-153`). This is the
DSP primitive — does the actual sample-rate conversion at any
ratio you give it.

### 3.2 `rmatch.c` — adaptive PI control loop

`create_rmatchV()` at `rmatch.c:501` wraps a `varsamp` with a
feedback ring buffer. The control law in `control()` lines 256-273:

```c
xaamav(a->ffmav, change, &current_ratio);           // moving avg of in/out ratio
current_ratio *= a->inv_nom_ratio;
a->feed_forward = ff_alpha * current_ratio
                + (1 - ff_alpha) * a->feed_forward; // FF term
deviation = a->n_ring - a->rsize/2;                 // ring fill error
xmav(a->propmav, deviation, &a->av_deviation);      // smoothed P term
a->var = a->feed_forward
       - a->pr_gain * a->av_deviation;              // PI-like update
// clamp [0.96, 1.04]
```

Called on every `xrmatchIN` (line 359, with `+insize`) and every
`xrmatchOUT` (line 464, with `-outsize`). Also includes:

- Crossfade `blend()` on overflow (`rmatch.c:275-283, 352`) —
  hides discontinuities when the ring overflows.
- Slewed silence-fill `dslew()` on underflow (`rmatch.c:364-425,
  438`) — graceful underrun recovery.

Operator-tunable knobs exposed via `ivac.c:757-849`:
`SetIVACFeedbackGain`, `SetIVACSlewTime`, `SetIVACPropRingMin/Max`,
`SetIVACFFRingMin/Max`, `SetIVACFFAlpha`. We won't expose all of
these in Lyra v0.0.9.6 — defaults work for >99% of operators —
but the structure is there if a tester needs to tune for an edge
case.

## 4. Lyra audio architecture (decision)

### 4.1 Two paths, hardware-aware default

| Hardware | Default path | Reason |
|---|---|---|
| HL2 (with onboard codec) | HL2 audio jack (HERMES-equivalent) | Single crystal, zero drift, zero CPU |
| HL2 (without codec, edge case) | PC Sound Card with rmatch | Codec unavailable |
| ANAN family (v0.4) | PC Sound Card with rmatch | No onboard codec |

This is implemented via the hardware capability struct (CLAUDE.md
§6.7). Protocol module sets `default_audio_path = HL2_CODEC` for
HL2; future ANAN protocol module sets `= PC_SOUND_CARD`. UI reads
from the struct for the operator's default. Operator can override
per-radio in Settings → Audio.

### 4.2 Path A — HL2 audio jack

Already implemented in Lyra under the misleading name "AK4951 mode"
through v0.0.9.5. **v0.0.9.6 renames** this to "HL2 audio jack" or
"HL2 codec" since not every HL2 revision uses the AK4951 chip
specifically (some have alternative codecs) but they all share the
same EP2-back-to-radio path.

Audio path (Lyra-native — modeled on Thetis HERMES, NOT a copy):

```
HL2 IQ  →  Channel.process()  →  StereoMixer.mix() (using lyra.dsp.mix:aamix port)
        →  Radio.send_audio_to_hl2()  →  HL2Stream EP2 frame writer
        →  EP2 packet (L/R int16 + TX I/Q)  →  HL2  →  codec  →  jack
```

The L/R audio bytes are already in the EP2 frame layout (CLAUDE.md
§3.4). The `aamix` port replaces the planned NumPy stereo mixer
in `lyra/dsp/mix.py` with a faithful WDSP equivalent. Pan curve
from `patchpanel.c::SetRXAPanelPan` makes RX1 hard-left / RX2
hard-right per §6.1 — same call site, drop-in.

### 4.3 Path B — PC Sound Card with rmatch + varsamp

```
HL2 IQ  →  Channel.process()  →  StereoMixer.mix()
        →  RmatchV.process(audio_block)  →  varsamp at adaptive ratio
        →  SoundDeviceSink ring buffer  →  WASAPI/PortAudio  →  PC speakers
```

`RmatchV` runs the PI control loop. Every output block, it
adjusts the resample ratio based on smoothed ring-fill deviation
+ measured input/output sample-count ratio. Clamps to ±4% of
nominal. This is what gives Thetis's ASIO path its glitch-free
behavior, and it's what Lyra's PC Sound Card mode has been
missing.

### 4.4 What we port and what we write Lyra-native

**Port directly from WDSP (with attribution per
`docs/architecture/wdsp_integration.md`):**

- `wdsp/aamix.c` → `lyra/dsp/mix.py` (audio mixer)
- `wdsp/varsamp.c` → `lyra/dsp/varsamp.py` (variable-ratio polyphase)
- `wdsp/rmatch.c` → `lyra/dsp/rmatch.py` (PI control loop wrapping varsamp)
- `wdsp/patchpanel.c::SetRXAPanelPan` → `lyra/dsp/mix.py` (pan curve)

**Write Lyra-native (study Thetis pattern, don't copy):**

- `lyra/audio/hl2_codec_sink.py` — sends L/R audio to HL2 via EP2
  (modeled on `network.c::SendpOutboundRx` + `networkproto1.c::
  sendProtocol1Samples` lines 1204-1267, but Lyra-native).
- `lyra/dsp/audio_sink.py` extension — wires `RmatchV` between
  the DSP chain and `SoundDeviceSink` for Path B.
- `lyra/audio/path_router.py` — picks Path A vs B based on
  operator preference + capability struct.

## 5. Why this took so long to get right

Honest retrospective for future-Claude / future-Rick:

1. **Wrong layer of investigation.** Multiple deep dives looked at
   producer-side timing (threading, MMCSS, EP2 cadence). The fix
   was always at the consumer side (audio output rate matching).
   None of the producer fixes addressed the underlying two-crystal
   drift.

2. **Hand-waving substitute for verification.** Earlier sessions
   claimed "Thetis benefits from WASAPI shared-mode resampling"
   without actually checking. The truth was that Thetis doesn't
   use WASAPI at all (it's `// not implemented`) and uses an
   explicit adaptive resampler in WDSP for the paths it does use.

3. **Hardware assumption errors.** Earlier sessions claimed "all
   HL2 variants have onboard codec" — operator pointed out the
   original HL2 doesn't have a speaker-out jack. The right framing
   is "HL2 with onboard codec → use HL2 audio jack; HL2 without →
   PC Sound Card with rmatch." Hardware capability struct in
   CLAUDE.md §6.7 captures this cleanly.

4. **Wrapper-thinking when porting was right.** Earlier sessions
   suggested wrapping `python-soxr` or `scipy.signal.resample_poly`
   instead of porting WDSP's proven implementation. Operator
   correctly pushed back: Lyra is GPL v3+, WDSP is GPL v3+, the
   AGC port worked perfectly, port the audio infrastructure too.
   "No more wrappers, no Hail Marys for things WDSP already
   solves" is the principle now.

5. **Agents weren't asked the right question.** Three engineers
   did deep dives on RX2 / TX / PureSignal yesterday but none was
   tasked with "what does Thetis use for general DSP rate-matching
   infrastructure?" varsamp/rmatch/aamix are foundational and
   would have surfaced if the question had been asked at the
   architecture level instead of the feature level.

The Thetis settings database from operator's daily-driver setup
plus a focused source-trace agent closed all five gaps in one pass.
Future investigations: ask the architectural-infrastructure
question first.

## 6. v0.0.9.6 implementation outline

Ordering (smallest → largest, lowest risk → highest):

1. **Branch `feature/v0.0.9.6-audio-foundation`** off main.
2. **Port `wdsp/patchpanel.c::SetRXAPanelPan`** (50 LOC) into a
   new `lyra/dsp/mix.py`. Pan curve only; no aamix yet.
3. **Port `wdsp/aamix.c`** (200 LOC) into the same `lyra/dsp/mix.py`.
   Two-channel stereo mixer with pan-aware RX1/RX2 placement.
4. **Port `wdsp/varsamp.c`** (400 LOC) → `lyra/dsp/varsamp.py`.
   Variable-ratio polyphase resampler. Bench test with synthetic
   sine + known ratio change.
5. **Port `wdsp/rmatch.c`** (700 LOC) → `lyra/dsp/rmatch.py`. PI
   control loop wrapping varsamp. Bench test with synthetic
   constant-rate-mismatch input — verify ring-fill convergence.
6. **Wire into audio output** — `lyra/dsp/audio_sink.py` (or new
   `lyra/audio/path_router.py`) routes between Path A (EP2 sender)
   and Path B (rmatch + ring + WASAPI).
7. **Default-flip + Settings UI** — operator's audio output
   preference defaults to "HL2 audio jack" for HL2 connections.
   Settings → Audio shows radio buttons.
8. **Operator A/B testing** — both paths flight-tested by Rick
   and Brent.
9. **CHANGELOG + version bump** (lyra/__init__.py + installer.iss).
10. **Ship v0.0.9.6** "Audio Foundation".

Estimated effort: 4-5 days focused work + 1 day operator testing.

## 7. What this enables downstream

The v0.0.9.6 audio infrastructure is foundational for later work:

- **v0.1 RX2:** stereo mix uses `mix.py::aamix`. RX2 falls into the
  existing audio path with no drama because the infrastructure is
  already there.
- **v0.2 TX:** PC mic input (when the operator opts in, or for
  ANAN later) uses `rmatch.py` on the input side — same control
  loop, opposite direction.
- **v0.3 PureSignal:** HL2 PS feedback at `rx1_rate` and ANAN PS
  feedback at fixed `ps_rate` both need rate conversion to the
  PS calc rate. `varsamp.py` handles both.
- **v0.4 ANAN:** ANAN has no onboard codec. PC Sound Card with
  rmatch is the canonical path. Capability struct sets it as
  default for ANAN models.

The audio question is answered. We use what works (WDSP), with
attribution, in a Lyra-native architecture. Done.
