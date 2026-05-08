# Measurements architecture & legacy DSP cleanup plan

*Created 2026-05-07 in response to two questions:*

> 1. *"The old code for sound — is it removed yet? If not, when?
>    Better to keep things clean than a jumbled mess."*
> 2. *"Check Thetis for how they're arriving at their S-meter and
>    calibration signals. Map out how RX, TX power, SWR, AGC, Mic
>    come into play — may save us headaches later."*

This doc is the answer plus the plan. Lives here so future Claude
sessions don't redo the research.

---

## Part 1 — Legacy DSP cleanup status & timeline

### What's still in `lyra/dsp/` and why

| File | Status in WDSP mode | Disposition |
| --- | --- | --- |
| `wdsp_native.py` | **Active** — cffi cdef + DLL loader | Keep |
| `wdsp_engine.py` | **Active** — high-level RxChannel wrapper | Keep |
| `audio_sink.py` | **Active** — AK4951Sink + SoundDeviceSink | Keep |
| `audio_mixer.py` | **Active** — AK4951 lockstep mixer | Keep |
| `worker.py` | **Active** — DSP worker thread coordinator | Keep |
| `binaural.py` | **Active** — runs as Python post-processing on WDSP stereo (item 5 of 2026-05-07 RX1 polish) | Keep |
| `rmatch.py` | **Active** — `WdspRMatch` class is the WDSP-backed wrapper, picked by `SoundDeviceSink`; pure-Python `RMatch` stays as fallback | Keep both classes; trim later |
| `varsamp.py` | **Fallback only** — only used by pure-Python `RMatch`; WDSP path bypasses it | Keep until pure-Python `RMatch` is dropped |
| `noise_profile_store.py` | **Active** — UI still saves/loads profile data; the audio path doesn't apply them in WDSP mode but the data layer is generic | Keep |
| `mix.py` | **Active** — RX1+RX2 stereo split routing (will matter once RX2 lands in v0.1) | Keep |
| `squelch.py` | **Active** — Lyra's noise-floor-based squelch is separate from WDSP's per-mode FM/AM squelch; they coexist (Lyra squelch can mute Lyra audio post-WDSP) | Keep |
| `agc_wdsp.py` | **Fallback only** — WDSP AGC subsumes it | Move to legacy |
| `nr.py` (NR1) | **Fallback only** — WDSP EMNR replaces it | Move to legacy |
| `nr2.py` (NR2) | **Fallback only** — WDSP EMNR replaces it | Move to legacy |
| `anf.py` | **Fallback only** — WDSP ANF replaces it | Move to legacy |
| `lms.py` | **Fallback only** — WDSP ANR replaces it | Move to legacy |
| `nb.py` | **Fallback only** — WDSP NOB/ANB now wired through `xnobEXT`/`xanbEXT` | Move to legacy |
| `demod.py` | **Fallback only** — WDSP demod replaces it | Move to legacy |
| `channel.py` | **Fallback only** — old per-RX decim+demod chain | Move to legacy |
| `leveler.py` | **Dropped** — WDSP AGC subsumes it | Move to legacy / candidate for delete |
| `apf.py` | **Deferred** — WDSP has SPEAK/MPEAK; will wire when CW work begins | Move to legacy until WDSP wiring lands |

### Why not delete now

1. **WDSP path is fresh.** RX1 went live 2026-05-06; only ~24 hours
   of operating before this RX1 polish push. Not enough field time
   to be confident there's no edge-case regression that needs the
   `LYRA_USE_LEGACY_DSP=1` bisect path.
2. **TX and PureSignal aren't on native yet.** v0.0.9.6 only covers
   RX1. Once TX (v0.2) and PS (v0.3) land on WDSP, the legacy
   modules are *truly* unreachable except via the fallback toggle.
   At that point the toggle itself becomes a candidate for retirement
   (or, more likely, conversion to a documented bug-bisect tool).
3. **Cleanup risk is asymmetric.** Deleting `nr.py` now and
   discovering an EMNR regression in 30 days = restore-from-git work
   on top of a real audio bug. Leaving them in tree costs nothing but
   ~50 KB on disk.

### Concrete cleanup timeline

**Phase A — now (post-RX1-polish, this commit):**
- Add a `lyra/dsp/legacy/` subpackage skeleton with a `README.md`
  explaining what lives there and why.
- Tag every legacy-only module's docstring with a `LEGACY:` header
  noting "active only when `LYRA_USE_LEGACY_DSP=1`."

**Phase B — after operator confirms WDSP-mode RX1 stable for ~30 days:**
- Move legacy modules into `lyra/dsp/legacy/`. Adjust imports
  (radio.py, worker.py).
- Delete `apf.py`/`leveler.py` if WDSP equivalents are wired in by
  then; else leave under `legacy/`.

**Phase C — after TX (v0.2) lands on WDSP:**
- Delete the WDSP-equivalent legacy modules outright if no
  regressions reported in v0.0.9.6 → v0.2 lifecycle.
- Decide whether to retain `LYRA_USE_LEGACY_DSP=1` as a bug-bisect
  feature or remove it. The latter simplifies the architecture; the
  former is operator-friendly when troubleshooting weird audio.

**Phase D — after PureSignal (v0.3) lands:**
- Final cleanup pass. By this point WDSP is the only path that's
  been touched in a year of operating; legacy modules are safely
  removable.

### What I will NOT do during cleanup

- **Don't delete `binaural.py`** — it ports as Python post-WDSP
  processing (already wired). It's not legacy.
- **Don't delete `noise_profile_store.py`** — operator-curated data
  store, still used by UI. The capture/save UX is decoupled from
  whether WDSP applies the profile.
- **Don't delete `squelch.py`** — Lyra's noise-floor-based RX
  squelch is a different feature from WDSP's per-mode FM/AM
  squelch. They serve different operator needs.
- **Don't delete `rmatch.py` / `varsamp.py`** — `WdspRMatch` lives
  in the same module as `RMatch`; the file isn't legacy. (Once the
  pure-Python `RMatch` is fully retired, `varsamp.py` can go.)

---

## Part 2 — Measurements architecture (RX + TX)

This is what each meter is, where the value comes from, and how
Lyra should deliver it. Format is same for each: **what**, **source
in Thetis** (so we know the proven path), **source in Lyra
v0.0.9.6**, **plan**.

### 2.1 RX measurements

#### Signal level (S-meter)

* **What.** dBm at the antenna feed, after LNA compensation.
  Operator reads it as "S-units" via a UI mapping (S9 = -73 dBm,
  6 dB per S-unit below S9, 10 dB per "S9+X" tick above).

* **Source in Thetis.** `WDSP.CalculateRXMeter(MeterType.SIGNAL_STRENGTH) + RXOffset(rx)` where
  `RXOffset(rx) = preamp_offset + (cal_offset + xvtr_gain + 6m_gain)`.
  `preamp_offset` is the LNA/attenuator setting in dB (e.g., 0 dB
  for HL2 LNA at full gain; +20 dB if a 20 dB step attenuator is
  inline). `cal_offset` is the operator's hand-tuned trim; for
  HL2, **Thetis's default is 0.98 dB** (essentially zero — WDSP's
  S-meter already reads correct dBm for HL2 hardware).

* **Source in Lyra v0.0.9.6.** FFT-derived: take FFT of recent IQ,
  pick passband bins, find peak (post-2026-05-07 fix; was integrated
  before), apply `_smeter_cal_db` (default +28 dB) minus current
  `_gain_db` (LNA gain). Custom code path.

* **Plan.** **Switch to WDSP's `RXA_S_PK` meter.** The cffi handle
  already exists (`RxChannel.get_meter(MeterType.S_PK)`); the
  conversion is "value + cal_offset_dB + lna_gain_dB" exactly like
  Thetis. The +28 dB cal trim drops to ~+1 dB. BW-invariance is
  built-in. This is a small commit, deferred from the 2026-05-07
  push because it deserves its own A/B test session against a
  reference signal.

  *Note*: a separate "spectrum scale" calibration (`_spectrum_cal_db`,
  default 0) keeps the panadapter dBFS scale independent of the
  S-meter — two different references, two different cal slots.
  No change to that path.

#### Noise floor (panadapter reference line)

* **What.** Estimate of ambient band noise in dBm. Operator sees
  it as a horizontal reference line on the panadapter.

* **Source in Thetis.** Computed from the panadapter spectrum
  (20th percentile of bins or similar). Cached and rolling-averaged.

* **Source in Lyra v0.0.9.6.** Same approach: 20th percentile of
  spec_db, EWMA over ~1 s. Already working.

* **Plan.** No change. The math is correct; the panadapter dBFS
  scale already has its own cal (`_spectrum_cal_db`). This meter
  doesn't go through WDSP at all.

#### AGC current gain

* **What.** dB of gain currently applied by AGC. Negative ⇒
  attenuating; positive ⇒ boosting; 0 ⇒ unity.

* **Source in Thetis.** `0 - WDSP.CalculateRXMeter(MeterType.AGC_GAIN)`.
  WDSP returns dB internally; Thetis negates because their UI
  shows AGC action with the inverted-sign convention (positive =
  more gain, but they want positive = more attenuation? — check the
  reference receiver if this becomes operator-confusing).

* **Source in Lyra v0.0.9.6.** Already wired:
  `_wdsp_rx.get_agc_gain_db()` (which calls
  `GetRXAMeter(channel, MeterType.AGC_GAIN)`), throttled to ~6 Hz,
  emitted on `agc_action_db`.

* **Plan.** Confirm sign convention matches operator expectations
  on a test signal. Lyra returns the value with no sign flip; if
  the operator wants Thetis-style negation, it's a one-line fix.

#### ADC peak/average

* **What.** Sample-level dBFS at the ADC. Used for LNA pull-up
  logic (decide when to bump LNA up because signal is strong).

* **Source in Thetis.** `WDSP.MeterType.ADC_REAL` /
  `MeterType.ADC_IMAG`.

* **Source in Lyra v0.0.9.6.** Computed in `_lna_loop` from the
  raw IQ samples directly, NOT from WDSP. Independent.

* **Plan.** No change for now. The Lyra-side calc is short and
  stays current with what's actually flowing through the network
  parser. Could move to WDSP's meter later for consistency, but
  no operator-visible benefit.

---

### 2.2 TX measurements (v0.2 onwards — sketch only for now)

Lyra doesn't ship TX yet. This section is the forward plan so we
don't paint into corners during v0.1 RX2.

#### Forward power (PA, watts at antenna)

* **What.** RF power at the antenna jack during transmit.

* **Source in Thetis.** Reads HL2's `getFwdPower()` (raw 12-bit ADC
  value from the SWR bridge), then maps:
  ```
  volts = (adc - adc_cal_offset) / 4095 * refvoltage
  watts = volts² / bridge_volt
  ```
  HL2 constants: `bridge_volt = 1.5`, `refvoltage = 3.3`,
  `adc_cal_offset = 6`. So a raw ADC reading of 100 ⇒ ~0.076 V ⇒
  ~3.8 mW (calibrated against the on-board HL2 power detector).

* **Source in Lyra v0.0.9.6.** Not yet implemented — TX is v0.2.

* **Plan for v0.2.** Port the same math into
  `lyra/protocol/p1_hl2.py` (HL2 quirks layer per CLAUDE.md §6.7).
  ADC value comes from EP6 status bytes (C&C frame 1, `prn->fwd`).
  Constants live in the protocol module, not in DSP.

#### Reflected power & SWR

* **What.** Reverse RF power at the antenna jack; SWR derived.

* **Source in Thetis.** `getRevPower()` raw ADC + same scaling
  approach. SWR formula:
  ```
  Ef = scaled_voltage(adc_fwd)
  Er = scaled_voltage(adc_rev)
  SWR = (Ef + Er) / (Ef - Er)
  ```
  Special cases: SWR=1.0 when both ADCs read 0 (no TX); SWR=50.0
  if reverse > forward (transmission line short or no antenna).

* **Plan for v0.2.** Same: live in `p1_hl2.py`. Surface as Qt
  signals: `tx_fwd_w_changed`, `tx_rev_w_changed`, `swr_changed`.
  SWR protection threshold (e.g., trip TX at SWR > 3.0) lives in
  Radio.

#### Mic input level

* **What.** dBFS of the audio coming into the modulator.

* **Source in Thetis.** WDSP's TXA chain has its own mic-input
  meter (TXA_MIC, etc.). Plus ALC and CFC stages with their own
  meters (TXA_LEVELER, TXA_COMP).

* **Plan for v0.2.** When TX wiring lands, expose
  `_wdsp_tx.get_meter(MeterType.MIC)` etc. on a dedicated
  `lyra/dsp/wdsp_tx_engine.py` (sibling to `wdsp_engine.py`).

#### ALC (Automatic Level Control) action

* **What.** dB of gain reduction applied by the TX's automatic
  leveler. Operator wants this near 0 dB (no leveler activity ⇒
  clean modulation); excursions to -10 dB indicate over-driving.

* **Plan for v0.2.** WDSP's TXA leveler exposes a meter. Wire it
  into the TX UI panel.

---

### 2.3 Why this architecture matters

A single guiding principle saves rework: **once a meter has a
canonical source, every UI surface (S-meter widget, status bar,
TCI sensor publish, hover tooltip) reads from that one source.**

Current Lyra has small drift between sources — the panadapter's
"signal strength" hover tooltip reads a per-bin dBFS while the
S-meter reads the integrated-power version, which used to be
~+17 dB different. Switching to WDSP's `RXA_S_PK` for the meter
+ keeping panadapter at per-bin dBFS gives:
- panadapter shows raw dBFS bins (operator's spectral picture)
- S-meter shows post-NBP0-passband signal level (operator's "what
  am I tuned to?" reading)
- Two values, two distinct meanings, no hidden math drift.

For TX, the analogous discipline is: forward-power source is the
HL2 EP6 ADC reading, scaled in the protocol layer, never
duplicated. SWR is a derived field on top — never a separate ADC
read. Keeping that discipline now (even before TX ships) means
the v0.2 TX panel is two days of UI work, not two weeks of "wait,
which forward-power number is this?"
