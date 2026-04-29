# WDSP & PureSignal — Integration Roadmap

**Status:** DESIGN DOC — companion to `threading.md`. No code yet.
**Author:** N8SDR + Claude
**Date:** 2026-04-29
**Target version range:** v0.1.x (TX path) → v0.2.x (PureSignal)

---

## 1. Why WDSP

WDSP (Warren Pratt's DSP library, used by Thetis / PowerSDR / CuSDR
and forks) is the most battle-tested ham-radio DSP backbone in
existence. Twenty years of refinement, deployed on tens of thousands
of stations, written in C with mature TX-side support. Reinventing
it in Python would be foolish for the heavy TX-side features.

**What we definitely need WDSP for** (not feasible to reinvent):

- **PureSignal** — adaptive TX predistortion using a feedback RX
  loop. WDSP has `xpsri`/`xpscbk`/PSR machinery that's been refined
  for a decade against real linear amps in real shacks. Building
  this from scratch is a years-long DSP-engineering project, not
  a feature ticket.
- **CESSB** — controlled-envelope SSB. Generates more talk power
  with the same average. Non-trivial DSP that operators expect on
  any modern HF SDR.
- **The whole TX modulation chain** — pre-emphasis, ALC, AM/FM/SSB
  modulators, sidetone for CW, etc. WDSP has all of this; we have
  none of it.

**What WDSP also has that we may want eventually:**

- NR2 (minimum-statistics noise reduction)
- NR3/NR4 (neural NR variants in some forks)
- Wideband AGC (whole-band leveling)
- Audio compressor / leveler
- VOX / break-in
- Diversity reception (combining RX1 + RX2)

**What we DON'T need WDSP for** (Lyra-native is fine or better):

- Demod (SSB / CW / AM / FM / DIG) — Lyra has these and they work
- Notch filters — Lyra's per-notch UI is arguably better than WDSP's
- Spectrum / waterfall — Lyra has GPU-accelerated rendering;
  WDSP has nothing comparable
- APF / BIN — Lyra has these
- AGC — Lyra has these (recalibrated in v0.0.5)
- Spectral-subtraction NR (NR1) — Lyra has this

So the integration philosophy is **selective adoption**, not
wholesale replacement.

## 2. The DspChannel abstraction (already designed for this)

The `DspChannel` abstract base class in `lyra/dsp/channel.py` was
written specifically with this future in mind. Two concrete
implementations will coexist:

```
                 ┌──────────────────┐
                 │  DspChannel ABC  │
                 │  (interface)     │
                 └────────┬─────────┘
                          │
           ┌──────────────┴──────────────┐
           │                              │
  ┌────────▼─────────┐         ┌─────────▼──────────┐
  │ PythonRxChannel  │         │   WdspChannel      │
  │ (today's stack)  │         │   (Phase WD-X)     │
  │                  │         │                    │
  │ - Pure Python    │         │ - ctypes / pybind  │
  │ - scipy/numpy    │         │   binding to       │
  │ - Lyra-native    │         │   wdsp.dll         │
  │   demods         │         │ - fexchange0()     │
  │ - NR, APF, BIN   │         │ - WDSP's full      │
  │ - Hackable +     │         │   feature set      │
  │   readable       │         │ - Battle-tested    │
  └──────────────────┘         └────────────────────┘
```

Operator picks which one is active via Settings → DSP →
"DSP engine: [Native ▼ | WDSP ▼]". Both are first-class supported
indefinitely. Reasoning:

- **Native** stays as the reference / debugging implementation. You
  can read every line of every demod. When something sounds wrong,
  you can step through the code.
- **WDSP** is for production users who want PureSignal, CESSB, and
  the polished TX experience.
- Cross-validation is its own win — A/B comparing WDSP vs Native on
  the same audio is a feature no other ham SDR offers.

## 3. WDSP integration phases

WDSP integration is **independent of Phase 3 threading** in concept,
but layers cleanly on top of it. The order matters for risk
management:

### Phase 3 — Threading (current focus)
Build the DspWorker thread architecture. This is the runway WDSP
will run on. **Status:** 3.A design doc complete, awaiting review.

### Phase WD-1 — WDSP RX integration (opt-in)
1. Add wdsp.dll bundling to the build pipeline (PyInstaller datas)
2. Build a Python ctypes binding for the subset of WDSP we need
3. Implement `WdspChannel(DspChannel)` — calls `OpenChannel()`,
   `fexchange0()`, `SetRXAMode()`, etc.
4. Settings → DSP → "DSP engine" combo: Native (default) | WDSP
5. Restart-to-switch (loading WDSP at runtime is doable but
   error-prone; restart is cleaner)

**Why opt-in:** WDSP is a C library — bugs are harder to chase, and
Lyra's appeal as a clean Python codebase shouldn't be compromised
unless the operator explicitly wants WDSP's features.

**What this gives operators:** WDSP's full RX-side feature set
including NR2, optional NR3/NR4 if a fork is used, wideband AGC,
diversity (when RX2 is wired). Native users keep what they have.

### Phase TX-1 — Lyra TX scaffolding (Native first)
Before PureSignal, we need a TX path at all. Lyra has zero TX
today. Sub-tasks (each its own commit):

1. `TxChannel` ABC + `PythonTxChannel` concrete (mic in → modulator
   → IQ out)
2. PTT state machine (semi-break-in, full break-in, manual PTT)
3. Modulators: SSB (USB/LSB), AM, FM, CW (key-down tone shaping)
4. ALC + ALC-clip protection
5. Sidetone for CW (fed back into the operator's audio sink during
   key-down)
6. TX-mode UI surface — Mic input source picker, mic gain, mic EQ
7. HPSDR Protocol 1 TX frame builder (we already have RX; TX is the
   complementary side of the same protocol)

This is **substantial new work** — multiple sessions, real audio
testing, careful protocol verification on the HL2.

### Phase WD-2 — WDSP TX integration
1. `WdspTxChannel(TxChannel)` — calls WDSP's TX API (`xen`, `OpenChannel`
   for TX, `xtx`, etc.)
2. Operator picks TX engine (Native | WDSP) — same pattern as RX
3. WDSP's CESSB, mic compressor, equalizer surface in Settings
4. ALC + level metering wired to WDSP's TX meters

### Phase PS-1 — PureSignal (depends on WD-2 + TX-1)
PureSignal needs:

1. **A feedback RX channel** — HL2 supports this via the
   `PURESIGNAL_RX` mode where the radio loops back attenuated TX
   signal into a special RX channel. Protocol-level work in
   `lyra/protocol/stream.py` to enable + parse this stream.
2. **WDSP's PSR (PureSignal) machinery** — `xpsr`, `xpscbk`,
   `SetPSRunCal`, etc. These functions take feedback IQ and
   compute predistortion coefficients in real time.
3. **Predistortion application** — the TX IQ stream goes through
   the PSR predistorter before reaching the protocol frame builder.
4. **Calibration UI** — operators run a calibration sweep on first
   use of PureSignal with their amp; coefficients persist per band.
5. **Operator UX** — PureSignal toggle on the front panel, "Cal
   Pure" button to start a calibration run, status display showing
   PS gain/state.

PureSignal is **Phase 0.2.x territory** — meaningful work, real
RF testing required, must not be rushed. Operators with linear
amps will love it; operators without won't notice. Worth doing
right when we get there.

### Phase NEURAL — Optional neural NR
Independent of Phase 3 / WD / TX / PS. Already a placeholder in
Lyra's NR profile menu. Lands when:

1. RNNoise or DeepFilterNet packaging stabilizes for Windows Python
2. We've collected operator feedback on classical NR + captured
   profile to know whether neural is worth the runtime cost

## 4. How Phase 3 threading helps WDSP

The DspWorker thread Lyra builds in Phase 3 is **exactly the
context WDSP wants to run in**. Thetis runs each WDSP channel on
its own thread; same pattern. So in Phase WD-1:

- The DspWorker stops calling `PythonRxChannel.process(iq)` and
  starts calling `WdspChannel.process(iq)` (or runs both side by
  side for A/B comparison)
- Worker config snapshot → marshal into WDSP's `SetRX*()` calls
- WDSP returns audio → same downstream path (audio sink, S-meter,
  spectrum)
- Reset/flush → WDSP's `OpenChannel(false, true, ...)` reset path

**Phase 3 threading is the right foundation regardless of WDSP.**
Native DSP benefits from it too; WDSP just slots in cleanly when
we're ready.

## 5. Building / packaging WDSP

WDSP is open-source C. Three deployment options to evaluate at
WD-1 kickoff:

1. **Bundle a pre-built wdsp.dll** with the Lyra installer.
   Simplest; what Thetis does. Downside: must be rebuilt for each
   architecture (we're x64 only, so just one build).
2. **Build from source on first launch.** Compiler dependency on
   the operator's machine. Painful.
3. **Vendor a Python ctypes / pybind11 wrapper.** If a maintained
   wrapper package exists for current WDSP, it's the cleanest;
   we'd add it to `requirements.txt` and operators get it via pip.
   **Investigation needed at WD-1 kickoff** — I'm not certain such
   a package exists today; building our own ctypes binding is the
   fallback.

**Decision deferred** to Phase WD-1 kickoff.

## 6. License compatibility — MUST VERIFY before WD-1

⚠️ **OPEN QUESTION.** I do not have current verified information
on WDSP's license terms. Before any WD-1 work begins, we must:

1. Read the WDSP source repository's LICENSE file directly
2. Confirm the license terms (MIT? BSD? Apache? GPL? LGPL?)
3. Verify compatibility with Lyra's MIT license

**If WDSP turns out to be GPL** (which is common for ham-radio C
libraries), bundling it with Lyra in a single installer may
trigger GPL copyleft obligations on Lyra itself, which we don't
want. Acceptable mitigations would be:

- **Keep WDSP as an external dependency** that operators install
  separately (no bundling — Lyra dynamically links/loads the DLL
  if present, falls back to native if not). This keeps Lyra MIT.
- **Distribute WDSP from a separate "Lyra-WDSP-bridge" repo**
  with its own license that's compatible with WDSP's. Lyra-MIT
  imports the bridge as a dependency.

**If WDSP is permissive** (MIT/BSD/Apache/etc.): bundling is fine,
attribution goes in `NOTICE.md`.

This is a **gating decision** for Phase WD-1 — we don't write any
ctypes binding code until the license question is settled. Until
then this doc assumes "permissive license, bundling allowed";
revise if reality differs.

## 7. Settings UX preview

Once Phase WD-1 lands, **Settings → DSP** gets a new section at
the top:

```
┌─ DSP Engine ───────────────────────────────────────────┐
│                                                          │
│   Engine: [● Native (Lyra)  ○ WDSP]                     │
│                                                          │
│   Native:                                               │
│   ✓ Pure Python — readable, hackable, no DLL deps       │
│   ✓ Demod (SSB/CW/AM/FM), NR1, APF, BIN, AGC            │
│   ✗ No PureSignal, no CESSB, no NR2/NR3/NR4             │
│                                                          │
│   WDSP:                                                 │
│   ✓ NR2 (minimum-statistics), NR3/NR4 if available      │
│   ✓ PureSignal (when a TX-capable HL2 + amp is wired)   │
│   ✓ CESSB, wideband AGC, audio compressor               │
│   ✗ More opaque (C library; harder to debug)            │
│                                                          │
│   Switching engines requires Lyra restart.              │
└─────────────────────────────────────────────────────────┘
```

Below that section, the existing AGC / NR / CW (APF/BIN) /
Captured Noise Profile groups appear. The active engine determines
which sub-controls are sensitive — e.g., "NR profile: Light/Medium/
Aggressive/Captured Profile" on Native; "NR mode: NR1/NR2/NR3/NR4"
on WDSP.

## 8. Migration / coexistence rules

- **Lyra-native stays the default** indefinitely. Operators get a
  working app out of the box without any DLL dependency.
- **WDSP is opt-in** — operator must explicitly switch engines.
- **Per-band engine memory? No (v1).** Engine choice is a single
  global preference. Per-band would let operators run Native on
  60 m and WDSP on 20 m, but the UX cost (engine restart on every
  band change?) doesn't justify it. Re-evaluate if testers ask.
- **Captured-noise-profile** lives in Native NR, not WDSP. WDSP
  has its own NR variants; capturing a profile is a Lyra-native
  feature. If operators want both, they can A/B between engines
  per-session.
- **Settings round-trip** — operator config (mode, BW, AGC profile,
  etc.) translates between engines. If you set USB + 2400 Hz BW on
  Native, then switch to WDSP, you get USB + 2400 Hz BW on WDSP.
  No surprise re-defaults.

## 9. Testing matrix (when WD-1 lands)

For each release that touches WDSP, smoke-test the matrix:

| Engine  | Mode | Sample rate | Notes |
|---------|------|-------------|-------|
| Native  | All  | 48k / 96k / 192k / 384k | Baseline, must regress nothing |
| WDSP    | All  | 48k / 96k / 192k / 384k | Verify equivalence |
| Switch  | USB  | 192k | Mid-session engine switch + restart |
| Switch  | CW   | 192k | Mid-session engine switch + restart |

Combined with Phase 3.C stress tests, this gives confidence both
engines work under load.

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| WDSP DLL build is hard on Windows | Low | Medium | Use Thetis's pre-built or community wrapper |
| ctypes bindings drift across WDSP versions | Medium | Medium | Pin to a specific WDSP commit; bump deliberately |
| WDSP's audio chain assumes 48k internally | Low | Low | Already true; we resample anyway |
| WDSP's threading model conflicts with our worker | Low | Medium | Worker owns its WDSP channel handle exclusively; no cross-thread WDSP calls |
| PureSignal calibration is ugly UX | Medium | Medium | Steal Thetis's UX shape — operators already know it |
| Operator confused by two engines | Medium | Low | Settings copy makes Native = "default for most users" clear |

## 11. Decision summary

| Question | Answer |
|---|---|
| Will Lyra integrate WDSP? | Yes, Phase WD-1+ |
| When? | After Phase 3 threading + after captured-noise-profile ships |
| Will it replace Lyra-native? | No. Both coexist long-term. Operator picks. |
| Required for PureSignal? | Practically yes. PureSignal via WDSP's PSR engine is the realistic path. A native (Python) PureSignal is theoretically possible (Volterra-series adaptive predistortion) but is a years-long DSP-engineering project not warranted unless WDSP integration becomes blocked. |
| Required for TX in general? | No. Native TX (Phase TX-1) lands first; WDSP TX (Phase WD-2) is the polished alternative. |
| Required for captured-noise-profile? | No. That's a Native-NR feature. |
| Required for ANF / NB / NR2? | No. Native-only versions land first; WDSP equivalents come along when WD-1 ships. |

## 12. Order of work — high level

Splitting smaller-than-originally-proposed so each release is
testable. Versions are markers, not commitments — we re-scope
per-release based on actual time taken.

```
v0.0.5  (✓ shipped) — Listening Tools (APF, BIN, GPU panadapter parity)
   │
v0.0.6 — Phase 3 threading (BETA toggle) — DSP worker thread
         shipped opt-in via Settings; default stays single-thread
   │
v0.0.7 — Captured-noise-profile NR (the headline ask) — runs on
         either backend
   │
v0.0.8 — NR2-native (minimum-statistics noise estimator)
   │
v0.0.9 — ANF (auto-notch, LMS adaptive)
   │
v0.0.10 — NB (impulse blanker, IQ-domain)
         At this point the noise toolkit is complete on Native.
   │
v0.1.0 — Native TX scaffolding (TX-1) — first transmit-capable Lyra
   │
v0.2.0 — WDSP integration (WD-1) — opt-in second DSP engine for RX
   │
v0.3.0 — WDSP TX (WD-2) + PureSignal calibration UX (PS-1)
   │
v0.4.0 — RX2 + diversity (depends on threading + WDSP being settled)
```

If at any point a release runs out of time, items slip to the
next without disrupting the order. Worker-thread default
promotion likely happens around v0.0.8 or v0.0.9 once it has 2-3
release cycles of operator field testing.

---

## Sign-off

**Operator (N8SDR):** [pending review]
**Lead:** Claude

When operator agrees with this layered plan, we proceed to Phase 3
implementation knowing WDSP slots in cleanly later. No need to
build WDSP scaffolding now — Phase 3 thread architecture is the
foundation; WDSP work comes when we're ready to lift TX off the
ground.
