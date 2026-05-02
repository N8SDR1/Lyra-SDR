# CLAUDE.md — Lyra-SDR project notes for Claude

This file is auto-loaded into Claude's context when working in the
Lyra-SDR repo.  It captures the core logic, key decisions, and
workflow rules so we don't have to re-research from scratch each
session.  Keep it concise — long-form research lives in
`docs/architecture/`.

When in doubt, consult:
- `docs/architecture/implementation_playbook.md` — current authoritative
  spec for RX2 / TX / PureSignal (v0.0.8 / v0.0.9 / v0.1).  **Start here**
  for any RX2/TX/PS implementation question.
- `docs/architecture/v0.0.8_rx2_plan.md` — phase plan, operator decisions.
- `docs/architecture/hl2_puresignal_audio_research.md` — HL2-specific
  PureSignal + audio chain research.
- `docs/architecture/rx2_research_notes.md` — first-pass Thetis
  research (some content superseded by the playbook; cross-reference).

---

## 1. Project at a glance

**Lyra-SDR** is a Qt6 / PySide6 desktop SDR transceiver for the Hermes
Lite 2 / 2+, written in Python.  Native HPSDR Protocol 1.

- **Target hardware**: Hermes Lite 2 / 2+ ONLY.  We do not support
  ANAN / Orion / Hermes / Hermes II.  Don't add code paths for them.
- **Author**: Rick Langford (N8SDR).  Memory note: nearby AM
  broadcaster causes 5th-harmonic interference on 7.250 MHz; factors
  into AGC / NR / notch defaults.
- **License**: GPL v3+ (since v0.0.6).  Was MIT through v0.0.5.
  Relicensed specifically to enable WDSP-derived code integration.
  See `NOTICE.md`.
- **Repo**: <https://github.com/N8SDR1/Lyra-SDR>.  Branches: `main`
  is the published release branch; `feature/threaded-dsp` is the
  active development trunk (kept fast-forward-able with `main`).
- **Current version**: 0.0.7 ("Polish Pass") — see `lyra/__init__.py`
  for the canonical version string.  Bump in one place; everything
  else follows.

## 2. License posture for WDSP ports

WDSP (by Warren Pratt NR0V, GPL v3+) is the openHPSDR DSP engine.
**Lyra is GPL-compatible with WDSP.**  Implications:

- We **may** port WDSP source directly into Lyra (Python or C
  extension).  Always include attribution comment with file path +
  line numbers.  See `docs/architecture/wdsp_integration.md` for the
  attribution template.
- We **may not** copy from Thetis's C# `Console\` code or
  `ChannelMaster\` C code — that's protocol/UI glue we should write
  Lyra-native, modeled on the pattern but not character-for-character.
- The line: WDSP DSP algorithms = port directly with attribution.
  Everything else = study the pattern, then write Lyra-native.

Already-ported WDSP modules in Lyra:
- `lyra/dsp/nr.py` (NR1 — spectral subtraction with Martin
  minimum-statistics, derived from `wdsp/anr.c` + `wdsp/emnr.c`)
- `lyra/dsp/nr2.py` (Ephraim-Malah / MMSE-LSA, derived from
  `wdsp/emnr.c`)
- `lyra/dsp/lms.py` (LMS adaptive line enhancer, derived from
  `wdsp/anr.c` Pratt 2012/2013 algorithm)
- `lyra/dsp/anf.py` (auto-notch filter, derived from `wdsp/anf.c`)
- `lyra/dsp/nb.py` (noise blanker)
- `lyra/dsp/squelch.py` (RMS + auto-tracked noise floor squelch)

## 3. HL2 protocol critical facts (don't forget these)

These are the gotchas that cost real debugging time when missed.

### 3.1 HL2 advertises `nddc = 4` on the wire

Even though HL2 silicon has only 2 physical DDC engines, the gateware
exposes 4 logical DDCs to the host.  Mapping:

```
DDC0 = RX1 frequency (VFO A)
DDC1 = RX2 frequency (VFO B)
DDC2 = TX frequency (used for PureSignal feedback during PS+TX)
DDC3 = TX frequency (used for PureSignal feedback during PS+TX)
```

For all RX2 work and beyond, `nddc=4` is the Lyra default for HL2.
The Hermes II `nddc=2` PS path is dead code on HL2 — don't add
special-case branches for it.

### 3.2 Frame 0 C4 byte mandatory bits

The "general settings" C&C frame (C0=0x00) C4 byte:

- bits[1:0] = antenna select (HL2 = 00, irrelevant)
- **bit 2 = duplex bit, ALWAYS 1** (HL2 quirk — without it, RX freq
  updates don't apply)
- bits[6:3] = `nddc - 1`.  For nddc=4: `(4-1) << 3 = 0x18`
- bit 7 = diversity (HL2 = 0)

Combined: `c4 = 0x1C` for nddc=4 + duplex bit set.

### 3.3 EP6 receive frame layout (nddc=4)

Per UDP datagram: 2 × 512-byte USB frames.  Per USB frame:

- bytes [0:3] = `0x7F 0x7F 0x7F` sync
- bytes [3:8] = C0..C4 (radio→host status: PTT, ADC overload, fwd/rev
  power, AIN voltages, optional I2C readback for HL2)
- bytes [8:512] = 504 bytes = 19 sample-slots × **26 bytes/slot**

Per 26-byte slot:
- bytes 0..2:  DDC0 I (BE 24-bit signed)
- bytes 3..5:  DDC0 Q
- bytes 6..8:  DDC1 I
- bytes 9..11: DDC1 Q
- bytes 12..14: DDC2 I
- bytes 15..17: DDC2 Q
- bytes 18..20: DDC3 I
- bytes 21..23: DDC3 Q
- bytes 24..25: mic sample (BE 16-bit signed)

Lyra's parser must skip DDC2/DDC3 bytes when PS is off (they're noise,
not useful).  The parser dispatches per-DDC into a callback like
`on_ddc_samples(ddc_idx, samples)`.

### 3.4 EP2 audio frame layout (host→radio)

Per UDP datagram: 2 × 512-byte USB frames.  Per USB frame:

- bytes [0:8] = control header
- bytes [8:512] = 504 bytes = 63 LRIQ tuples × **8 bytes/tuple**

Per 8-byte tuple:
- bytes 0..1: L audio (BE 16-bit signed)
- bytes 2..3: R audio
- bytes 4..5: TX I (BE 16-bit signed)
- bytes 6..7: TX Q

Quantization: `int16 = round(sample * 32767)` with explicit
floor/ceil for round-to-nearest.

### 3.5 HL2 audio rate is fixed at 48 kHz

The on-board AK4951 codec is hard-locked at 48 kHz by the gateware.
EP2 LRIQ tuples produce one set of L/R audio + I/Q TX per USB frame.
HL2's TX I/Q rate is also 48 kHz (no resampling needed in TX path).

### 3.6 RX I/Q rates can differ between DDCs

Per Thetis's `cmaster.c::SetDDCRate(i, rate)`, each DDC can run at
its own rate (48k / 96k / 192k / 384k).  Lyra's existing decimator
in `lyra/dsp/channel.py` already handles arbitrary input rates →
fixed audio rate, so per-DDC rate independence is "free" for v0.0.8
(no new code needed).

### 3.7 PureSignal is one bit (well, three)

To enable PureSignal:
- `nddc = 4` (HL2 default already)
- frame 0 C4 bit 2 = 1 (duplex bit, always set anyway)
- frame 11 C2 bit 6 = 1 (`puresignal_run`)
- frame 16 C2 bit 6 = 1 (`puresignal_run`)

Thetis sets the bit, then trusts the gateware to deliver feedback
samples on DDC2/DDC3.  No protocol-level handshake or status
read-back.  HL2 community gateware variant + hardware mod handles
the rest.

### 3.8 HL2 quirks vs ANAN

- **TX attenuator range = -28..+31 dB** (not 0..31).  Negative
  values are gain rather than attenuation.  Used for both normal TX
  gain and PS auto-attenuator state machine.
- **CWX PTT bit on HL2 = bit 3 in I-sample LSB** (standard HPSDR
  uses only bits 0..2).
- **L/R audio channels can be swapped** by some HL2 firmware revs.
  Add a `swap_lr_audio` Settings option to compensate.
- **HL2 read-loop handles I2C readback inline** — when C0 has bit 7
  set, frame data is I2C response, not ADC overload status.
- **PS sample rate during PS+TX** = `rx1_rate` (whatever user
  selected), NOT the 192 kHz `ps_rate` ANAN uses.  Thetis comment:
  "HL2 can work at a high sample rate."
- **PS auto-attenuate recalibrate trigger**: `FeedbackLevel > 181 ||
  (FeedbackLevel <= 128 && cur_att > -28)`.

## 4. WDSP port strategy (concrete)

### 4.1 Port directly with attribution

| WDSP file | Lyra target | Effort | Phase |
|---|---|---|---|
| `patchpanel.c::SetRXAPanelPan` (50 LOC) | `lyra/dsp/mix.py` (pan curve) | 1 hour | v0.0.8 |
| `compress.c` (~150 LOC) | `lyra/dsp/tx_compressor.py` | 1 day | v0.0.9.1 |
| `lmath.c::xbuilder` (~200 LOC) | `lyra/dsp/ps_xbuilder.py` | 2 days | v0.1 |
| `delay.c` (~80 LOC) | `lyra/dsp/delay_line.py` | 4 hours | v0.1 |
| `iqc.c` (315 LOC) | `lyra/dsp/ps_iqc.py` | 4 days | v0.1 |
| `calcc.c` (1164 LOC) | `lyra/dsp/ps_calcc.py` | 2 weeks | v0.1 |

### 4.2 Write Lyra-native (don't port)

These are Thetis-specific glue or trivially small:

- `TXA.c`, `RXA.c` — channel scaffolding.  Lyra has its own.
- `channel.c` — buffer mgmt.  Python's GIL handles it.
- `aamix.c` — mixer.  Replace with NumPy in `lyra/dsp/mix.py`.
- `analyzer.c` — spectrum.  Lyra has its own GPU widget.
- `main.c` — Win32 thread mgmt.  Use Python threading.

### 4.3 Don't reach for cffi/WDSP DLL until profiling forces it

Pure Python with NumPy comfortably handles 192k I/Q + 48k audio per
RX, dual-RX, with overhead.  C extensions add wheel-build complexity
that conflicts with Lyra's "pip install and go" ethos.

## 5. Lyra threading model

Five threads across the v0.0.8 / v0.0.9 / v0.1 roadmap:

```
Thread 1: HL2Stream._rx_loop          (recvfrom loop)
Thread 2: DSP worker                   (RX1 + RX2 chains, audio sink, TX chain in v0.0.9)
Thread 3 (NEW in v0.1): PS calc thread (semaphore-driven, runs calc())
Thread 4: HL2Stream TX writer          (drains TX queue at EP2 cadence)
Thread 5: Qt main thread               (UI; signals/slots only)
```

**No MMCSS / OS thread priority** for v0.0.8.  Python's GIL is the
binding constraint, not OS priority.  Add MMCSS only if profiling
shows audio drops.

**Buffer flow contract** (RX side, v0.0.8):

```
HL2Stream._rx_loop  → parser splits to {0,1,2,3}
                    → on_ddc_samples(ddc=0, ...) → Radio.dispatch_rx1
                    → on_ddc_samples(ddc=1, ...) → Radio.dispatch_rx2
                    → on_ddc_samples(ddc=2, ...) → drop (v0.0.8) / PS feedback (v0.1)

Radio.dispatch_rx*  → DspChannel[k].process(iq) → audio_k
                    → both audios in hand → StereoMixer.mix() → stereo
                    → audio_sink.write(stereo)
```

dispatch_rx1 and dispatch_rx2 fire on the **same parser invocation**
in sequence.  Both produce equal-length audio (decimators map any IQ
rate → fixed audio rate).  No queueing latency, no cross-thread
fan-out.

## 6. Core architecture decisions (settled)

### 6.1 RX2 audio routing

**Stereo split via EP2 LR bytes through the AK4951 codec.**  RX1
hard-left, RX2 hard-right.  Auto-applied when RX2 enables.

- Per-RX `pan` parameter, default 0.5.  When RX2 enables: RX1.pan=0,
  RX2.pan=1.
- Pan curve: WDSP sin-π rule (port from `wdsp/patchpanel.c`).  At
  pan=0.5, both channels at unity (6 dB louder than endpoints).
  Don't use Lyra's existing equal-power Balance rule; use WDSP's.
- L/R swap option in Settings (HL2 firmware-rev compensation).
- No host-side sounddevice path for v0.0.8 — AK4951 is the canonical
  HL2 audio route.

### 6.2 RX2 UI model — hybrid

- Each RX has its own freq display + panadapter region with
  read-only status badges (mode, filter, AGC).
- Single MODE+FILTER and DSP+AUDIO panels operate on the **focused
  RX**.
- Click any freq display to focus.  Hotkeys: Ctrl+1 → RX1, Ctrl+2 →
  RX2.
- Focus indicator: colored border on focused freq display + matching
  control panel header tint.

### 6.3 SPLIT semantics

- VFO A = RX1 freq (always).
- VFO B = RX2 freq when RX2 is enabled, otherwise a "shadow" freq.
- SPLIT toggle: TX freq = VFO B's freq when ON, VFO A's when OFF.
- VFO B lock toggle prevents accidental tuning during pile-up
  listening.
- Buttons: A→B, B→A, Swap.
- TX cursor renders on whichever RX shows the TX VFO (in v0.0.8 even
  before TX itself ships).

### 6.4 DDC frequency-source abstraction

```python
ddc[0].freq_source = "VFOA"   # RX1 — always VFOA
ddc[1].freq_source = "VFOB"   # RX2 — always VFOB
ddc[2].freq_source = "TX"     # PS feedback in v0.1; static TX in v0.0.8
ddc[3].freq_source = "TX"     # Same
```

DDC2/DDC3 always carry TX freq in C&C frames 5/6 regardless of PS
state.  Parser must always skip those bytes.  When v0.1 lands and
sets `puresignal_run=True`, the same freq writes become "PS feedback
freq" — no protocol redesign.

### 6.5 PureSignal posture

- Plumb the protocol surface in v0.0.8 (`puresignal_run` flag in C&C
  writer, DDC freq-source abstraction).  Inert in v0.0.8.
- v0.1 = port `calcc.c` + `iqc.c` + supporting modules.
- Operator self-attestation that they have the HL2 PS hardware mod
  installed.  Settings checkbox: "I have the PureSignal hardware mod
  installed."  Default OFF; until checked, PS controls disabled with
  explanatory tooltip.
- N8SDR runs PS on HL2/HL2+ with appropriate gateware + mod.  This is
  the working configuration.

### 6.6 PTT state machine (v0.0.9)

States: RX → MOX_TX (UI button or CAT) → CW_TX (key down) → TUN_TX
(low-power tune) → VOX_TX (deferred to v0.1).

- RX-mute fade ~50 ms when MOX→TX (no clicks).
- Hardware PTT input via HL2 EP6 status bytes (`prn->ptt_in =
  ControlBytesIn[0] & 0x1`).
- State machine in `lyra/radio/ptt.py`.  Qt signal `mox_changed
  (bool)` for UI.

## 7. Phased delivery roadmap

### v0.0.8 — RX2

- Phase 0: multi-channel refactor (no behavior change).
- Phase 1: protocol RX2 enablement (nddc=4, EP6 parser rewrite).
- Phase 2: stereo split audio routing.
- Phase 3: UI integration (focus model, hotkeys, A↔B/Swap/Lock buttons).
- Phase 4: split panadapter (vertical splitter in central widget).
- Phase 5: polish, persistence, docs.
- Rolling pre-releases per phase.

### v0.0.9 — TX (post-RX2)

- v0.0.9.0: SSB only (USB/LSB) + PTT + drive level + fwd/rev power.
- v0.0.9.1: CW (with internal keyer + sidetone, CWX PTT bit), AM,
  compressor port from WDSP.
- v0.0.9.2: FM, CFC.
- v0.0.9.3: Leveler, equalizer.

### v0.1 — PureSignal

- Port `calcc.c` + `iqc.c` + `xbuilder` + `delay.c`.
- New `PSDialog` UI modeled on Thetis's `PSForm.cs`.
- Auto-attenuator state machine (HL2-specific bounds).
- Coefficient persistence to `~/.config/lyra/ps_corrections/`.
- Operator self-attestation checkbox.

## 8. File path conventions

```
lyra/
├── __init__.py                    # version source of truth
├── radio.py                       # Radio class — channel dict + facades
├── protocol/
│   └── stream.py                  # HPSDR P1 — nddc=4, per-DDC freq, etc.
├── dsp/
│   ├── channel.py                 # per-RX DSP chain (existing)
│   ├── mix.py                     # NEW v0.0.8 — StereoMixer + WDSP pan curve
│   ├── tx_channel.py              # NEW v0.0.9 — TX DSP chain
│   ├── ssb_mod.py                 # NEW v0.0.9 — SSB modulator
│   ├── cw_keyer.py                # NEW v0.0.9.1
│   ├── tx_compressor.py           # NEW v0.0.9.1 — port from compress.c
│   ├── ps_calcc.py                # NEW v0.1 — port from calcc.c
│   ├── ps_iqc.py                  # NEW v0.1 — port from iqc.c
│   ├── ps_xbuilder.py             # NEW v0.1 — cubic-spline coef builder
│   └── delay_line.py              # NEW v0.1
├── radio/
│   └── ptt.py                     # NEW v0.0.9 — PTT state machine
├── ui/
│   ├── panels.py                  # extend for RX2/TX/PS controls
│   ├── spectrum.py                # add split-vertical mode for dual pan
│   └── ps_dialog.py               # NEW v0.1 — modeled on PSForm.cs

docs/architecture/                  # research + plans (this conversation)
├── implementation_playbook.md     # AUTHORITATIVE — start here
├── v0.0.8_rx2_plan.md
├── hl2_puresignal_audio_research.md
├── rx2_research_notes.md
├── threading.md                   # existing
├── noise_toolkit.md               # existing
└── wdsp_integration.md            # existing — attribution patterns
```

## 9. Reference paths in Thetis source tree

When I need to verify a protocol detail mid-implementation:

```
D:\sdrprojects\OpenHPSDR-Thetis-2.10.3.13\Project Files\Source\
├── ChannelMaster\
│   ├── networkproto1.c            # HL2 read/write loops, EP2/EP6 packing
│   ├── cmaster.c                  # WDSP per-receiver setup
│   └── network.h                  # struct definitions, bit fields
├── Console\                       # C# UI + radio control (DON'T copy code)
│   ├── console.cs                 # UpdateDDCs, AAmixer states
│   ├── PSForm.cs                  # PS state machine, HL2 attenuator bounds
│   ├── radio.cs                   # WDSP channel ID convention
│   └── HPSDR\IoBoardHl2.cs        # I/O board context
└── wdsp\                          # GPL v3+, OK to port
    ├── calcc.c, calcc.h           # PS calibration
    ├── iqc.c, iqc.h               # PS predistortion application
    ├── patchpanel.c               # pan curve (port for mix.py)
    ├── compress.c                 # TX compressor (port for v0.0.9.1)
    ├── lmath.c                    # xbuilder cubic-spline (port for v0.1)
    ├── delay.c                    # delay line (port for v0.1)
    └── (137 other files)          # consult as needed
```

Specific landmarks worth remembering:

- `networkproto1.c::WriteMainLoop_HL2` lines 869–1201 — full C&C
  frame schedule
- `networkproto1.c::MetisReadThreadMainLoop_HL2` lines 422–586 —
  EP6 receive parsing
- `networkproto1.c::sendProtocol1Samples` lines 1204–1267 — EP2
  audio packing
- `console.cs::UpdateDDCs` lines 8214–8577 — DDC enable / sample-rate
  per model
- `console.cs::UpdateAAudioMixerStates` lines 28217–28333 — audio mix
  routing
- `PSForm.cs::timer1code` lines 553–727 — PS state machine
- `PSForm.cs::timer2code` lines 728–820 — auto-attenuator (HL2-specific)
- `PSForm.cs::NeedToRecalibrate_HL2` line 1142 — HL2 recal threshold
- `wdsp/patchpanel.c::SetRXAPanelPan` lines 158–176 — pan curve
- `wdsp/calcc.c::calc()` lines 324–483 — predistortion math
- `wdsp/iqc.c::xiqc()` lines 122–203 — predistortion application

## 9.5. NR audit follow-up notes (operator-confirmed)

From the NR audit (`docs/architecture/nr_audit.md`) §9 open questions:

- ~~**AC mains frequency at N8SDR's QTH: 60 Hz** (US standard).
  When cyclostationary 60/120 Hz powerline modeling lands (audit
  §4.3(c)), it must be operator-configurable...~~  **OBSOLETE —
  cyclostationary modeling is NOT being pursued.**  See next bullet.

- **CYCLOSTATIONARY POWERLINE MODELING (P2) NOT PURSUED
  (2026-05-02).**  Reviewed after the P1.3 auto-select deferral
  and dropped on operator judgment: "got us into some hopes that
  won't pan out in real-world operator mode."  Reality check —
  AC mains drift (60 ±0.05 Hz under load), the lack of a direct
  line-phase reference at 48 kHz audio, the actual non-coherence
  of typical powerline noise sources (arcing contacts, motor
  commutators, dimmer SCRs each on their own phase), and the
  operator-tunes-around behavior all conspire against the
  audit's optimistic 10-20 dB gain estimate.  Real gain probably
  3-5 dB over the existing Wiener-from-profile path that already
  ships in v0.0.7.x.  Not worth the complexity / schema-bump /
  profile-invalidation risk.  See `docs/architecture/nr_audit.md`
  §4.3(c) STATUS block for the full reasoning.

- **NR polish strategy chosen: P1 (auto-select / staleness /
  smart-guard) → P2 (cyclostationary) → P3 trickles in.**  Skipping
  ML-based VAD (i) since auto-select reduces live-source usage.
  Skipping (j) cross-channel validation pending RX2.

- **AUTO-SELECT EXPLICITLY DEFERRED INDEFINITELY (2026-05-02).**
  Operator decision after senior-engineering review of the
  proposed implementation: captured profiles are operator-curated
  by design (each station / location / operator is unique;
  operator ears pick up things the algorithm can't).  Algorithmic
  auto-select — even in "suggest" mode — overrides operator
  choice with a spectral-distance metric and creates UX noise
  without delivering value.  See `docs/architecture/nr_audit.md`
  §4.3(a) STATUS block for the full reasoning.

  What stays in scope for the captured-profile feature:
    * Operator-driven explicit blending (manual slider in manager)
    * Diagnostic readouts ("this profile is X dB different from
      current band noise") — informational, operator decides
    * Smart-guard improvements (already shipped P1.1)
    * Staleness toast notifications (already shipped P1.2 —
      passive notification, operator decides whether to recapture)

  Out of scope:
    * Any feature where Lyra picks a profile FOR the operator
    * Suggestion toasts the algorithm initiates
    * The math module `lyra/dsp/noise_profile_match.py` was
      prototyped briefly and **removed** as part of the same
      decision — keeps the "no auto-comparison code" principle
      enforced at the file-system level.

## 9.6. Audio-pops quiet-pass v0.0.7.1 (shipped 2026-05-02)

Operator-reported "consistent random pops, some many dB above
audio level."  Senior-engineering audit produced
`docs/architecture/audio_pops_audit.md`; three P0 fixes shipped on
`feature/v0.0.7.1-quiet-pass`:

- **P0.1** AGC per-sample envelope tracker (eb437ae) — replaces
  block-scalar AGC.  Eliminated the loud multi-dB pops.  See
  `_apply_agc_and_volume` + `_refresh_agc_per_sample_constants`
  in `lyra/radio.py`.  Bench: 1 kHz step-amplitude sine, boundary
  step dropped from 0.029 -> 0.0041 (= natural sine slope).
  CPU: ~0.11 ms/block (0.5% of 21 ms budget).
- **P0.2** Preserve decimator state across `channel.reset()`
  (3d0ba70) — was rebuilding the FIR from zeros on every
  freq/mode change, producing a click on every tune.  Bench:
  boundary step 0.100 -> 0.013, recovery 1.35 ms -> 0 ms.
- **P0.3** AK4951 sink-swap 5 ms fade-out (244a8b2) — added
  `HL2Stream.fade_and_replace_tx_audio()` and updated
  `AK4951Sink.close()` to fade gracefully instead of flipping
  `inject_audio_tx = False` instantly.

**Operator flight-test result (2026-05-02):** "noticeably better,
loud spikes gone, but occasional pops/clicks slightly louder than
the rest of audio still happen."

### Residual clicks — PARKED for future investigation

Diagnosis state at park time:
- Reproducible with **all DSP off** (NB / NR / ANF / LMS / SQ /
  APF) at 192 kHz LSB / 2.4 kHz filter.
- **Reproducible into a 50-ohm dummy load** (no antenna), so it's
  not atmospheric / RF / lightning / static.
- Network ruled out: dedicated direct-wired NIC to HL2, lowest
  Windows route metric, no WiFi.
- Most likely remaining sources (in priority order):
  * **HL2 hardware/gateware glitches** — ADC sample dropouts,
    DDC numerical edges, USB-to-ethernet bridge buffer hiccups.
    Specific to N8SDR's HL2+ unit; may differ on other boards.
  * **Python GIL / GC pauses** starving the audio thread, causing
    EP2 underrun and audible step at the underrun-recovery
    boundary.  Plausible but unverified.
  * **Per-sample AGC + Rayleigh noise tail** — the new instant-
    attack tracker can briefly clamp gain on random thermal-
    noise envelope excursions; subsequent samples then show a
    drop in output.  Step is small (~0.02 amplitude) but maybe
    audible on a quiet listening session.

Diagnostic instrumentation already in place
(`set LYRA_AUDIO_DEBUG=1` env var, commit e535db7):
`Radio._diagnose_audio_step` prints one rate-limited log line per
audio block whenever the post-AGC output has a sample-to-sample
step exceeding 0.05 amplitude.  Includes index, prev/curr output,
input mag, peak, gain, and peak ratio at the offending sample.
Use this when picking the investigation back up — operator runs
with the env var, we correlate timestamps with audible clicks,
then implement the targeted fix (e.g., look-ahead AGC, GIL hold-
off, gateware-version triage).

When circling back: read this section, then
`docs/architecture/audio_pops_audit.md` §3 (P1 / P2 suspects we
explicitly didn't ship in v0.0.7.1 but may revisit here).

## 10. Open empirical questions (need HL2+ bench testing)

These weren't answered by code-reading; we'll find out on N8SDR's
hardware:

1. **HL2 mic samples in EP6 with AK4951 audio active** — value or
   zero?  Affects v0.0.9 mic-input source choice.
2. **DDC2/DDC3 sample rate during PS+TX** — Thetis sets RX1 rate but
   actual gateware delivery is TBD.  Wireshark a PS+TX session.
3. **HL2 PA-on bit power-up default** — is `pa & 1` set by gateware
   on power-up, or do we need to assert it?
4. **PA fwd/rev power calibration constants** — vary per HL2 board
   revision.  Operator self-cal in Settings → TX is the right answer.
5. **N8SDR's specific HL2+ gateware version** — document for future
   reference.
6. **AK4951 EP2 cadence behavior** — does HL2 gateware drop or buffer
   EP2 frames over the 48 kHz cadence?  Affects TX queue throttling.

## 11. Workflow conventions

### Branching

- `main` = published release branch, fast-forward-able with
  feature/threaded-dsp.
- `feature/threaded-dsp` = active dev trunk.
- New feature work: create `feature/<topic>` off
  feature/threaded-dsp; merge back when stable.

### Commits

- Use conventional summary line ("RX2: ...", "TX: ...", "PS: ...")
  for easy grep.
- Include "Co-Authored-By: Claude Opus 4.7" trailer per existing
  pattern.

### Releases

- Single-source version: `lyra/__init__.py` + `build/installer.iss`.
- Update `CHANGELOG.md` (consolidated; replaces per-version
  RELEASE_NOTES files).
- Annotated tag (`git tag -a v0.0.X`).
- Build via `build/build.cmd` (PyInstaller + Inno Setup).
- Draft GitHub Release manually with installer .exe attached.

### Pre-releases for tester feedback

- Cut pre-releases per phase during long features (worked well for
  v0.0.6 / v0.0.7).
- v0.0.8 phases: 0 (refactor), 1 (protocol), 2 (audio), 3 (UI),
  4 (panadapter), 5 (polish).  One pre-release per phase.

## 12. How to point Claude back to these docs

When starting a new session for RX2/TX/PS implementation work, you
can prompt me with any of:

- **"Read CLAUDE.md"** — auto-loaded, but you can ask me to re-read
  it explicitly if you want me to refresh.
- **"Read docs/architecture/implementation_playbook.md"** — full
  authoritative spec.
- **"Read the RX2 research notes"** / **"Read the PS research"** —
  the longer-form research documents.
- **"What does Thetis do for X in HL2?"** — I'll either remember from
  these docs or grep the Thetis tree at
  `D:\sdrprojects\OpenHPSDR-Thetis-2.10.3.13\`.
- **"Show me the WDSP source for X"** — I'll read from
  `D:\sdrprojects\OpenHPSDR-Thetis-2.10.3.13\Project Files\Source\wdsp\`.

For specific implementation work, give me the phase number from §7
and I'll know what's in scope.  For example: "Start v0.0.8 Phase 0"
means multi-channel refactor with no behavior change.

When something I do conflicts with this doc, **trust this doc over
my session memory** — this is the consolidated source of truth.  If
this doc is wrong, we update it explicitly.

---

*Last updated: 2026-05-02 after the senior-engineering pass that
produced `implementation_playbook.md`.  Update this file when key
decisions change.*
