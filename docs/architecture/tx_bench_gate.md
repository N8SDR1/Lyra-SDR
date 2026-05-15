# TX chain bench gate (v0.2 Phase 2 commit 11)

Two-tier validation procedure for the v0.2 TX I/Q producer chain.
There is **no Tier C** — no precision-test-gear dependency exists
anywhere in the Lyra validation story (PureSignal is self-measuring
via the HL2 PA-coupler feedback path; see "Why no Tier C" below).

## Tier A — software self-check (no HL2 / no PTT / no RF)

Runnable any time:

```
python -m scratch.test_tx_chain_bench
```

Validates the pieces unit tests can't (the real bundled WDSP DLL
producing sane I/Q) plus the integration glue end-to-end:

| Check | What it proves |
|-------|----------------|
| TxChannel opens + IM-5 init applied | WDSP TXA channel construction + the mandated setter sequence |
| WDSP TXA produces non-zero I/Q from a 1 kHz tone | The DSP chain (panel → phrot → bp0 → ALC → output) actually modulates |
| TX I/Q is analytic (non-zero Q) | Real SSB, not a degenerate real-only signal |
| MoxEdgeFade ramps 0→1 over 2400 samples | Commit 10 anti-click envelope |
| Sip1Tap write→snapshot round-trip | Commit 9 PS calibration tap |
| EP2 packs TX I/Q into slot bytes 4..7 | Commit 8 byte packing |
| inject_tx_iq=False → cols 2..3 zero | v0.1.1 wire-parity guarantee |

**This is the Phase 2 completion gate.** Phase 2 is "done" when
Tier A is fully green + the IM-5 setter audit passes.

### IM-5 setter audit (software, runnable now)

Per CLAUDE.md §15.18 pre-cdef discipline, row-by-row verify the
`wdsp_tx_engine.py` `_apply_init_setters` sequence against the
WDSP source at `D:\sdrprojects\OpenHPSDR-Thetis-2.10.3.13`.

**Audit run 2026-05-15 — RESULT: CLEAN.** All 17 IM-5 setters
present with correct signatures:

* `SetTXAALCAttack/Decay/Hang(int channel, int ms)` — int, not
  double sec (the v0.0.9.8.1 `SetRXAAGCSlope` register-class bug
  class is absent)
* `SetTXAALCMaxGain` / `SetTXALevelerTop(int, double)` — double
* `SetTXAPHROT*` — UPPERCASE confirmed (case-sensitive C symbols)
* `SetTXAALCThresh` — confirmed absent from `wcpAGC.c`; correctly
  NOT called
* cffi cdefs in `wdsp_native.py` match WDSP source byte-for-byte
  on parameter types

## Tier B — gross RF sanity (Phase 3 PTT + dummy load + SDRPlay)

When Phase 3 PTT lands and `inject_tx_iq` flips True on MOX=1:

1. Dummy load on the HL2 antenna port.
2. SDRPlay on the bench (near-field; a degraded antenna input or
   a short clip lead is fine — HL2 TX leakage at a few feet is
   strong).
3. Key PTT, whistle / talk into the mic.
4. Confirm on the SDRPlay spectrum:
   * It's SSB (suppressed carrier, voice sidebands), not a
     carrier or garbage
   * Correct sideband (USB: energy above suppressed carrier;
     LSB: below)
   * **No key-click** at PTT keydown/keyup (commit 10 MoxEdgeFade
     working — this is the operator-audible proof of the 50 ms
     cos² envelope)
   * Occupied bandwidth ~2.7 kHz, not smeared across 20 kHz
     (gross splatter check)

Precision IMD/ACPR is **not** part of this — that's a PureSignal
(v0.3) concern, and even then it's self-measured (see below).

## Why no Tier C

PureSignal is a **closed-loop self-measuring system**. The HL2's
PA-coupler feedback path (DDC0/DDC1 per CLAUDE.md §3.8) is the
measurement instrument. The v0.3 `calcc.c` algorithm measures the
PA's own nonlinearity using the radio's own ADC and computes the
correction internally. Ham PS validation uses:

1. PS's own internal feedback metrics (built into v0.3 code)
2. The same Tier-B SDRPlay near-field for PS-off vs PS-on
   IMD-shoulder A/B (the shoulders drop 20–40 dB — a dramatic,
   obvious visual change requiring no calibration accuracy
   because it's a relative comparison)
3. On-air signal reports

External precision gear would only be for publishing an absolute
calibrated ACPR number — a spec-sheet claim Lyra has no need to
make. No precision-gear dependency exists anywhere in the
validation story.

## Current status (2026-05-15)

**Tier A: GREEN.** All 7 checks pass. Resolved 2026-05-15 per
CLAUDE.md §15.23 — root cause was an extraneous
`SetTXABandpassRun(ch,1)` call toggling WDSP's stale
compressor-only `bp1` into the SSB path (NOT a dead input path;
the early "mic input produces zero I/Q" framing was the
misdiagnosis the §15.23 trail documents). Fix: removed the
call + centralized the per-mode sign in
`TxChannel._signed_edges`/`_push_bandpass_locked`. Verified:
non-zero I/Q peak 0.545, analytic mean|Q| 0.258; FFT bench
(`test_tx_dsp_bench.py`) OVERALL PASS — USB/LSB mirror-symmetric,
69 dB sideband / 63 dB carrier suppression; 60/60 TX unit tests
green.

> *Archaeology trailer:* this section read "Tier A: RED — mic
> input path produces zero I/Q" from 2026-05-15 morning until
> the §15.23 3-agent investigation root-caused it that
> afternoon. The gate did its job — it caught a non-functional
> TX chain *before* Phase 3 was built on it. Kept here so a
> future reader who finds an old reference to "Tier A RED"
> knows it was resolved, not abandoned.

Tier B + the PS A/B: deferred to Phase 3 PTT (cannot key without
it) and v0.3 respectively.
