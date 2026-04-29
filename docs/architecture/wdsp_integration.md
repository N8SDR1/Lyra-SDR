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
1. Add `wdsp.dll` bundling to the build pipeline (PyInstaller datas)
2. Build a Python ctypes binding for the subset of WDSP we need
3. Implement `WdspChannel(DspChannel)` — calls `OpenChannel()`,
   `fexchange0()`, `SetRXAMode()`, etc.
4. Settings → DSP → "DSP engine" combo: Native (default) | WDSP
5. Restart-to-switch (loading WDSP at runtime is doable but
   error-prone; restart is cleaner)

**Why opt-in (Native stays default):** purely technical preferences,
not licensing — both engines are now GPL-compatible:

- **Smaller installer** — bundling `wdsp.dll` is fine (~3-5 MB) but
  operators on dial-up / metered connections appreciate the
  zero-DLL Native footprint
- **Easier debugging** — Native is pure Python; operators can step
  through a demod or NR stage with any debugger. WDSP is a C
  library; debugging requires C tools.
- **Faster bring-up** — Native works immediately; WDSP needs a one-
  time DLL load + channel-handle setup. Operators just trying Lyra
  for the first time get the simpler path.
- **Cross-validation** — having both engines means operators can
  A/B compare. That's a feature in itself.

**What this gives operators:** WDSP's full RX-side feature set
including NR2, optional NR3/NR4 if a fork is used, wideband AGC,
diversity (when RX2 is wired). Native users keep what they have
plus the noise toolkit we're building on the Native path
(captured-noise-profile, NR2-native, ANF, NB).

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

With license compatibility resolved (Section 6), the deployment
question becomes purely operational. Three options ranked:

### Option 1 (RECOMMENDED) — Bundle a pre-built `wdsp.dll`
- What Thetis does
- Add `wdsp.dll` to `build/lyra.spec`'s `datas` list (same way we
  bundle the GPU shaders)
- Operators get one `Lyra-Setup-X.Y.Z.exe` download; everything
  works
- Build pipeline rebuilds `wdsp.dll` from a pinned upstream commit
  whenever we want to upgrade
- We're x64-only on Windows so just one build artifact

### Option 2 (FALLBACK) — Build from source on first launch
- Compiler dependency on operator's machine — painful
- Only worth doing if Option 1 hits a roadblock

### Option 3 (BACKUP) — Vendor a Python wrapper package
- If a maintained `wdsp-py` (or similar) package exists, we could
  add it to `requirements.txt` and operators get it via pip
- **Investigation needed at WD-1 kickoff** — I'm not certain a
  current Python wrapper exists. If one does, evaluate it; if
  not, building our own ctypes binding is the path

**Recommended decision: Option 1 (bundle), with our own ctypes
binding for the small WDSP API surface we actually use.**

Concrete WDSP API surface we'd bind initially (RX-side only):
`OpenChannel`, `CloseChannel`, `fexchange0`, `SetRXAMode`,
`SetRXAFiltLow/High`, `SetRXAAGCMode`, `SetRXAANRvals`,
`SetRXAEMNRRun`, `SetEXTANBRun`, `SetEXTNOBRun`. ~15 functions, all
documented in WDSP's headers.

## 6. License compatibility — RESOLVED ✓

**Status: license question resolved 2026-04-29.**

- **WDSP** is licensed under **GNU GPL v2 or any later version**
  (verified by reading source-file headers in the TAPR/OpenHPSDR-wdsp
  repository).
- **Thetis** is GPL v2 (or later, per WDSP's terms).
- **Lyra** as of v0.0.6 is **GPL v3 or later** (relicensed from
  MIT specifically to enable openHPSDR ecosystem integration).

These licenses are mutually compatible. Lyra (GPL v3+) can directly
use WDSP (GPL v2 or later → upgradeable to v3) with no licensing
gymnastics required. Direct linking, bundling in the installer,
shipping wdsp.dll alongside Lyra.exe — all permitted.

### What this means in practice

- **Bundle wdsp.dll directly** with the Lyra installer
- **Direct linking via ctypes** is fine
- **No subprocess/sidecar workaround** needed
- **Attribution required** in `NOTICE.md` (which already lists
  WDSP) and any source files we incorporate keep their headers
- **Modifications to WDSP** that we ship must be available in
  source form, just like our own source

The earlier draft of this doc assumed Lyra was MIT-only and laid
out elaborate workarounds (subprocess sidecar). That version is
superseded — the GPL relicense (effective v0.0.6) makes Phase WD-1
straightforward.

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

- **Lyra-native stays the default** for new installs. Operators
  who want WDSP's specific feature set (NR2/NR3, PureSignal, CESSB)
  flip a Settings toggle. This isn't a license-driven choice (both
  engines are GPL-compatible now); it's about giving operators a
  no-DLL-dependency starting experience and reserving WDSP for
  operators who specifically want its features.
- **WDSP is opt-in** — operator must explicitly switch engines.
- **Per-band engine memory? No (v1).** Engine choice is a single
  global preference. Per-band would let operators run Native on
  60 m and WDSP on 20 m, but the UX cost (engine restart on every
  band change?) doesn't justify it. Re-evaluate if testers ask.
- **Captured-noise-profile** is a Lyra-native NR feature, separate
  from WDSP's NR variants. The two are different design ideas:
  captured-profile lets the operator record their specific QRM and
  target it; WDSP NR2/NR3 are statistical estimators that adapt
  automatically. Operators who want captured-profile use Native;
  operators who want WDSP's algorithmic NRs use WDSP. We could
  later add captured-profile *to* WDSP if there's demand, but
  Native-only for v1.
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

### Could WD-1 move earlier?

With license compatibility resolved, **WD-1 (RX-side WDSP
integration) is no longer technically blocked.** The current
ordering (Native noise toolkit first, then TX scaffolding, then
WDSP) reflects operator priority — captured-noise-profile was the
headline ask, and native ANF / NB / NR2 round out the toolkit
operators expect on the Native engine.

If priorities shift (e.g., several testers ask for WDSP's NR2/NR3
sooner, or a request for PureSignal becomes urgent), WD-1 can
move earlier without rework — both engines coexist by design.
Specifically, WD-1 could insert anywhere after v0.0.6 (threading)
since the worker thread is the only WDSP prerequisite.

**Practical guidance:** keep the Native-first ordering until
operator field reports tell us to change. Don't reorder
speculatively.

---

## Sign-off

**Operator (N8SDR):** Reviewed 2026-04-29 — approved direction.
Lyra relicensed MIT → GPL v3+ (effective v0.0.6). WDSP integration
unblocked technically; held back to follow operator-priority order
(noise toolkit first, then TX, then WDSP).
**Lead:** Claude

**Status:** Both architecture docs (threading.md + this file) are
now coherent post-relicense. Phase 3 implementation can proceed
knowing:

1. Threading uses a Settings-toggle approach (Single = default,
   Worker = BETA) — see `threading.md` §13
2. WDSP integration slots into the same DspWorker thread when WD-1
   lands — no further architectural decisions needed before then
3. License compatibility is resolved; bundling `wdsp.dll` with
   Lyra is permitted

No further design work is gating Phase 3.B. We start with B.1
(DspWorker shell) when the operator gives the go-ahead.
