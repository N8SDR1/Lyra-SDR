# CLAUDE.md — Lyra-SDR project notes for Claude

This file is auto-loaded into Claude's context when working in the
Lyra-SDR repo.  It captures the core logic, key decisions, and
workflow rules so we don't have to re-research from scratch each
session.  Keep it concise — long-form research lives in
`docs/architecture/`.

When in doubt, consult:
- `docs/architecture/implementation_playbook.md` — current authoritative
  spec for RX2 / TX / PureSignal (v0.0.9 / v0.1 / v0.2).
- **`docs/architecture/v0.1_rx2_consensus_plan.md`** — AUTHORITATIVE
  consensus plan from the 2026-05-05 three-engineer review (protocol,
  DSP, UI) with two rounds of cross-validation.  **Open this FIRST**
  for any v0.1 / v0.2 / v0.3 implementation question.  Supersedes
  `v0.0.9_rx2_plan.md` and `rx2_research_notes.md`.  Contains: locked
  channel ID convention, phase-by-phase plan, mandatory bench-test
  gates for v0.3 PS, errors corrected from round 1 to round 2,
  patterns Lyra adopts vs deliberate Thetis divergences.
- `docs/architecture/v0.0.9_rx2_plan.md` — superseded.  Historical.
- `docs/architecture/hl2_puresignal_audio_research.md` — HL2-specific
  PureSignal + audio chain research.
- `docs/architecture/rx2_research_notes.md` — superseded by the
  consensus plan above.  Historical first-pass research.

**Version-numbering note (2026-05-03):** version numbering has
shifted twice during the v0.0.7 → v0.0.9 cycle.  Current state:

- **v0.0.8** "Quiet & Polish Pass" — audio quiet pass + notch v2
  + click-to-tune (shipped 2026-05-02).
- **v0.0.8.1** — auto-update notification fix (2026-05-02).
- **v0.0.9** "Memory & Stations" — operator-driven pre-RX2
  polish: TIME button, GEN customization, Memory bank, EiBi
  shortwave broadcaster overlay (shipped 2026-05-02).
- **v0.1**   = RX2 (was originally v0.0.9; shifted when the
  Memory & Stations batch landed).
- **v0.2**   = TX.
- **v0.3**   = PureSignal.
- **v0.4**   = Multi-radio refactor + Protocol 2 + ANAN family
  (operator decision 2026-05-03; see §7 + §6.7).

References to the old numbering in commit history / older doc
revisions are historical and intentionally not back-edited.  Doc
content below has been mass-renumbered to the new scheme.

**Subsequent patch releases (2026-05-05 / 06):**
- **v0.0.9.4** "Polish & Notifications" — watermark bundling fix,
  first-time-per-version update modal, toolbar pulse, Settings
  dialog lambda crash fixes.
- **v0.0.9.5** "Captured-Profile UX" — smart-guard removed (false
  positives + false negatives in field testing), tunable staleness
  threshold, live drift readout in profile manager, TCI server +
  profile manager dialog stability fixes.
- **v0.0.9.6** "Audio Foundation" (shipped 2026-05-08) — wholesale
  pivot from pure-Python DSP to cffi calls into the WDSP DSP engine
  for the RX1 audio chain.  Per-sample numpy work in legacy modules
  (agc_wdsp / nr / nr2 / anf / demod / channel) was producing GIL
  contention with the EP2 writer thread, manifesting as HL2
  audio-jack clicks and PC Soundcard motorboating.  WDSP DLLs
  bundled at `lyra/dsp/_native/`.  Cleanup arc retired ~6,800
  lines of legacy DSP code (Audio Leveler, agc_wdsp, apf, demod,
  nb, lms, anf, squelch, nr2, PythonRxChannel.process, etc.).
  See §13 (audio architecture), §14 (WDSP-DLL integration), §14.9
  (cleanup arc).
- **v0.0.9.9.1** "Launch Hotfix" (2026-05-10) — emergency
  patch over v0.0.9.9.  Two fixes:
  (1) ``faulthandler.enable()`` added in v0.0.9.9 raised
  ``RuntimeError: sys.stderr is None`` at import time on the
  PyInstaller ``--windowed`` build (Lyra.exe ships with
  ``console=False``, so sys.stderr is None).  Anyone who
  downloaded v0.0.9.9 from GitHub couldn't launch Lyra.
  Bench didn't catch it because source-tree runs have a real
  stderr.  Fix routes crash output to
  ``%APPDATA%\Lyra\crash.log`` (operator-visible artifact for
  bug reports); falls back to sys.stderr if the file can't be
  opened; silent no-op if neither works (won't crash launch).
  (2) Brent reported EiBi overlay missing on Software /
  OpenGL graphics backends.  The QPainter SpectrumPanel setup
  was missing the four-signal EiBi wiring block that the GPU
  SpectrumPanel had — the renderer was correct, but
  ``_refresh_eibi_overlay`` was never connected so
  ``set_eibi_entries(...)`` was never called.  Fix mirrors the
  GPU section's wiring into ``_setup_qpainter_panadapter``;
  operator confirmed working on all three backends.  v0.0.9.9
  GitHub release retracted after v0.0.9.9.1 publishes.
- **v0.0.9.9** "IQ Captured Profiles" (2026-05-10) — §14.6
  IQ-domain captured-profile rebuild lands LIVE.  Replaces the
  v0.0.9.6-era "capture works, apply is INERT in WDSP mode"
  state with a full pre-WDSP IQ-domain pipeline: capture taps
  raw IQ at the operator's native rate, apply runs Wiener-from-
  profile spectral subtraction on raw IQ before WDSP's RXA
  chain.  Sidesteps the AGC-mismatch that broke three rounds
  of post-WDSP audio-domain attempts.  Schema bump to v2
  (rate-specific full complex-FFT magnitudes); v1 audio-domain
  profiles refused on load with recapture hint.  New STFT
  engine (sqrt-Hann WOLA, COLA-1 exact, Wiener gain with
  temporal smoothing) in ``lyra/dsp/captured_profile_iq.py``;
  10/10 synthetic-bench validation.  Phase 5 UX: Switch
  profile right-click submenu (single-click reload), gain
  smoothing slider (live-tunable, default γ=0.6 ~10 ms time
  constant), FFT size dropdown (1024/2048/4096), badge
  tooltip refresh.  Crash fix: ``_iq_capture_lock`` extended
  to cover WDSP close+null and worker's ``_wdsp_rx.process()``
  call, closing the TOCTOU race that produced silent crashes
  on rapid rate-change cycles.  ``faulthandler.enable()``
  added permanently for general crash forensics.  Operator
  field-tested through 3+ rate cycles with captured profile;
  watery character "light, becomes inaudible after a minute"
  per operator with γ=0.6 default.
- **v0.0.9.8.1** "AGC + persistence patch" (2026-05-10) —
  substantial bug-fix patch over v0.0.9.8.  Headline: a
  latent ``SetRXAAGCSlope`` cffi binding bug from v0.0.9.6
  was caught by an audit of every cffi binding's parameter
  types vs. the WDSP C source — only one mismatch found
  (the binding declared ``double slope`` while the C
  function is ``int slope``, producing a register-class
  calling-convention bug on Windows x86_64 → garbage
  ``var_gain`` → ``max_gain`` pinned at random value →
  AGC profile time constants masked).  Fix made
  AGC profiles audibly distinct for the first time since
  v0.0.9.6.  Plus per-band waterfall + spectrum scale
  persistence repair (apply_current_band_range public
  method + spectrum autoload from_user=False + auto-scale
  waterfall protection); per-mode RX bandwidth
  persistence (was never saved/loaded); AGC threshold UX
  modernization (legacy 0..1 linear field repurposed as
  dBFS, Settings slider replaced by label + Auto button,
  Auto reads live noise floor); AGC slope default 0 → 35
  (industry soft-knee convention); ``Long`` AGC profile
  restored to UI menu; click-to-tune snap polish (SNR
  threshold 6→8 dB, 2 kHz effective-range cap); CLAUDE.md
  §15.1/§15.5 closed, §9.8 withdrawn.
- **v0.0.9.8** "Display Polish" (CW VFO convention switch,
  2026-05-10) — operator-visible behaviour change for CW
  operators: the VFO LED now shows the **carrier frequency**
  of the tuned signal, matching the standard convention used
  across major HF SDR applications.  This replaces the v0.0.9.7.x
  filter-zero convention where the LED showed (carrier − pitch)
  for CWU and various tuning surfaces (click-to-tune, NCDXF
  marker click, NCDXF auto-follow, TCI spot click) each had to
  apply the CW pitch offset themselves.  v0.0.9.8 puts the
  offset CENTRALLY in radio.py (``_compute_dds_freq_hz`` helper
  called by ``set_freq_hz`` / ``set_mode`` / ``set_cw_pitch_hz``)
  so every freq write to the protocol layer is automatically
  offset for CW; all per-call-site offsets are reverted.  The
  spectrum widget receives DDS as its center_hz, and a new
  ``marker_offset_hz`` (= VFO − DDS) shifts the orange marker
  line to the operator's tuned carrier — visually right of
  center for CWU, left for CWL, at center for non-CW.  CW Zero
  white reference line removed (redundant under new
  convention).  v0.0.9.7.2 was committed and tagged but NOT
  released to GitHub — its TCI-spot per-call-site fix was
  superseded by this convention switch.  Saved CW freqs from
  v0.0.9.7.x will display ``pitch`` Hz off until retuned once;
  no auto-migration (operators in active testing retune
  naturally).
- **v0.0.9.7.2** "Display Polish" (TCI CW spot tuning fix,
  2026-05-10) — patch over v0.0.9.7.1.  Companion to the NCDXF
  fix; same class of issue, different tuning surface.  TCI CW
  spots forwarded by SDRLogger+ (and every cluster / RBN /
  Skimmer source it upstreams from) carry the **carrier**
  frequency — clicking them in CWU/CWL previously landed at
  zero-beat.  ``radio.activate_spot_near`` now subtracts pitch
  for CWU / bare "CW" spots and adds pitch for CWL spots; non-
  CW spots untouched.  ``spot_activated`` signal still emits
  the original carrier freq so TCI round-trip is preserved.
  Verified SDRLogger+ source at ``Y:/Claude local/hamlog/
  main.py:3808+`` does no mode-aware adjustment — passes
  upstream cluster freq through unchanged, locking the
  carrier-freq convention between the two sibling apps.
  Convention documented in `docs/help/tci.md`.
- **v0.0.9.7.1** "Display Polish" (NCDXF tuning fix, 2026-05-09)
  — patch over v0.0.9.7.  Bug fix only: NCDXF beacon tuning
  (marker click + auto-follow) now applies the CW pitch offset,
  so the operator hears the beacon at their configured pitch
  tone instead of zero-beat.  Two surgical edits to
  `_on_landmark_clicked` (panels.py) and `_ncdxf_follow_pump`
  (radio.py) following the same offset pattern `_on_click`
  already used for plain click-to-tune.
- **v0.0.9.7** "Display Polish" (2026-05-09) — operator-driven UX
  polish on spectrum/waterfall/Display panel surfaces, plus
  Settings dialog stability hardening.  New operator-facing
  features: Peak Hold combo (Off/Live/timed/Hold) + Decay
  (Fast/Med/Slow) + Clear button on the Display panel; Exact /
  100 Hz tuning quantization toggle; Spec/WF zoom slider
  live-preview during drag; spectrum trace fill master toggle +
  custom fill color picker; waterfall collapse toggle; per-band
  waterfall min/max persistence (sister to per-band spectrum
  bounds).  Bug fixes: dB-lock recall on restart, Settings dialog
  dead-widget guards (`_safe_mirror`, `_swallow_dead_widget`),
  wrapped-label squeeze fix on Noise + Visuals tabs, dialog size
  bump 1100×760 → 1280×880, custom-color button width 120 → 140
  px.  Documentation pass aligned help docs with the NR-mode UX
  overhaul + AGC profile + ANF profile-name corrections from the
  v0.0.9.6 cleanup arc.  See §15 for the residual doc backlog
  parked for future cleanup.

---

## 1. Project at a glance

**Lyra-SDR** is a Qt6 / PySide6 desktop SDR transceiver for the Hermes
Lite 2 / 2+, written in Python.  Native HPSDR Protocol 1.

- **Target hardware (current, v0.0.x → v0.3)**: Hermes Lite 2 / 2+
  ONLY.  Don't add ANAN / Orion / Hermes / Hermes II code paths
  during this phase — but **do** write hardware-agnostic code
  wherever feasible (see §6.7).
- **Future hardware support (v0.4)**: Protocol 2 + ANAN family
  (G2 / G2-1K / 7000DLE / 8000) is on the long-term roadmap per
  operator decision 2026-05-03.  v0.1 / v0.2 / v0.3 stay
  HL2-only by scope, but the hardware-abstraction discipline in
  §6.7 prevents painting into a corner.
- **Author**: Rick Langford (N8SDR).  Memory note: nearby AM
  broadcaster causes 5th-harmonic interference on 7.250 MHz; factors
  into AGC / NR / notch defaults.
- **Audio testing methodology (operator note 2026-05-06):** Rick
  has very good hearing/ears.  For RX-side audio A/B he runs
  Windows with **all "audio enhancements" disabled** (no loudness
  equalization, bass boost, virtual surround, etc.) — no Windows-
  side coloration.  His standard is "what's produced is what
  should be heard naturally."  When he reports coloration on a
  Lyra audio path, it's coming from Lyra (or the audio device
  itself), NOT from Windows enhancements.  Important context for
  audio-quality investigations: don't reach for "check your
  Windows enhancements" as a first hypothesis.
- **License**: GPL v3+ (since v0.0.6).  Was MIT through v0.0.5.
  Relicensed specifically to enable WDSP-derived code integration.
  See `NOTICE.md`.
- **Repo**: <https://github.com/N8SDR1/Lyra-SDR>.  Branches: `main`
  is the published release branch; `feature/threaded-dsp` is the
  active development trunk (kept fast-forward-able with `main`).
- **Current version**: see `lyra/__init__.py` for the canonical
  ``__version__`` + ``__version_name__`` strings.  The
  version-numbering history near the top of this file lists the
  delivered releases through to today.  Bump in one place
  (``__init__.py`` + ``build/installer.iss``); everything else —
  About dialog, status bar, installer filename, GitHub release
  tag — follows.

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
fixed audio rate, so per-DDC rate independence is "free" for v0.0.9
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
| `patchpanel.c::SetRXAPanelPan` (50 LOC) | `lyra/dsp/mix.py` (pan curve) | 1 hour | v0.0.9 |
| `compress.c` (~150 LOC) | `lyra/dsp/tx_compressor.py` | 1 day | v0.1.1 |
| `lmath.c::xbuilder` (~200 LOC) | `lyra/dsp/ps_xbuilder.py` | 2 days | v0.2 |
| `delay.c` (~80 LOC) | `lyra/dsp/delay_line.py` | 4 hours | v0.2 |
| `iqc.c` (315 LOC) | `lyra/dsp/ps_iqc.py` | 4 days | v0.2 |
| `calcc.c` (1164 LOC) | `lyra/dsp/ps_calcc.py` | 2 weeks | v0.2 |

### 4.2 Write Lyra-native (don't port)

These are Thetis-specific glue or trivially small:

- `TXA.c`, `RXA.c` — channel scaffolding.  Lyra has its own.
- `channel.c` — buffer mgmt.  Python's GIL handles it.
- `aamix.c` — mixer.  Replace with NumPy in `lyra/dsp/mix.py`.
- `analyzer.c` — spectrum.  Lyra has its own GPU widget.
- `main.c` — Win32 thread mgmt.  Use Python threading.

### 4.3 cffi + WDSP DLL — adopted 2026-05-06

**Earlier guidance** in this section said: "Don't reach for cffi /
WDSP DLL until profiling forces it. Pure Python with NumPy
comfortably handles 192k I/Q + 48k audio per RX." That guidance
turned out to be optimistic.  Profiling DID force it: per-sample
work in agc_wdsp / nr / nr2 / anf / demod / channel produced GIL
contention with the EP2 writer thread that surfaced as audio
clicks and motorboating.  We tried surgical fixes for several
rounds before pivoting.

**Current direction (v0.0.9.6+):** the RX (and eventually TX, RX2,
PureSignal) DSP chain is implemented as cffi calls into the WDSP
DSP engine — Lyra-relevant entry points declared in
`lyra/dsp/wdsp_native.py`, high-level wrapper in
`lyra/dsp/wdsp_engine.py`, native binaries bundled at
`lyra/dsp/_native/` so installs don't depend on any other radio
program being present on the operator's machine.

**License posture:** Lyra is GPL-3.0-or-later, the bundled DSP
engine is also GPL-3.0-or-later — link-compatible.

**Wheel-build complexity worry:** the bundled-DLL approach
sidesteps it entirely. The five DLLs ship with Lyra; cffi loads
them at runtime. No compiler invocation at install or runtime.

**The pure-Python DSP modules in `lyra/dsp/` stay in tree** as a
LYRA_USE_LEGACY_DSP=1 fallback and as the basis for DSP layers
that don't overlap WDSP (the spectrum widget, captured noise
profiles UX, click-to-tune, etc.).  Cleanup pass after the native
engine is solid through TX + PureSignal.

See §14 below for the actual integration architecture.

## 5. Lyra threading model

Five threads across the v0.0.9 / v0.1 / v0.2 roadmap:

```
Thread 1: HL2Stream._rx_loop          (recvfrom loop)
Thread 2: DSP worker                   (RX1 + RX2 chains, audio sink, TX chain in v0.1)
Thread 3 (NEW in v0.2): PS calc thread (semaphore-driven, runs calc())
Thread 4: HL2Stream TX writer          (drains TX queue at EP2 cadence)
Thread 5: Qt main thread               (UI; signals/slots only)
```

**No MMCSS / OS thread priority** for v0.0.9.  Python's GIL is the
binding constraint, not OS priority.  Add MMCSS only if profiling
shows audio drops.

**Buffer flow contract** (RX side, v0.0.9):

```
HL2Stream._rx_loop  → parser splits to {0,1,2,3}
                    → on_ddc_samples(ddc=0, ...) → Radio.dispatch_rx1
                    → on_ddc_samples(ddc=1, ...) → Radio.dispatch_rx2
                    → on_ddc_samples(ddc=2, ...) → drop (v0.0.9) / PS feedback (v0.2)

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
- No host-side sounddevice path for v0.0.9 — AK4951 is the canonical
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
- TX cursor renders on whichever RX shows the TX VFO (in v0.0.9 even
  before TX itself ships).

### 6.4 DDC frequency-source abstraction

```python
ddc[0].freq_source = "VFOA"   # RX1 — always VFOA
ddc[1].freq_source = "VFOB"   # RX2 — always VFOB
ddc[2].freq_source = "TX"     # PS feedback in v0.2; static TX in v0.0.9
ddc[3].freq_source = "TX"     # Same
```

DDC2/DDC3 always carry TX freq in C&C frames 5/6 regardless of PS
state.  Parser must always skip those bytes.  When v0.2 lands and
sets `puresignal_run=True`, the same freq writes become "PS feedback
freq" — no protocol redesign.

### 6.5 PureSignal posture

- Plumb the protocol surface in v0.0.9 (`puresignal_run` flag in C&C
  writer, DDC freq-source abstraction).  Inert in v0.0.9.
- v0.2 = port `calcc.c` + `iqc.c` + supporting modules.
- Operator self-attestation that they have the HL2 PS hardware mod
  installed.  Settings checkbox: "I have the PureSignal hardware mod
  installed."  Default OFF; until checked, PS controls disabled with
  explanatory tooltip.
- N8SDR runs PS on HL2/HL2+ with appropriate gateware + mod.  This is
  the working configuration.

### 6.6 PTT state machine (v0.1)

States: RX → MOX_TX (UI button or CAT) → CW_TX (key down) → TUN_TX
(low-power tune) → VOX_TX (deferred to v0.2).

- RX-mute fade ~50 ms when MOX→TX (no clicks).
- Hardware PTT input via HL2 EP6 status bytes (`prn->ptt_in =
  ControlBytesIn[0] & 0x1`).
- State machine in `lyra/radio/ptt.py`.  Qt signal `mox_changed
  (bool)` for UI.

### 6.7 Hardware abstraction discipline (for v0.4 ANAN work)

Operator decision 2026-05-03: ANAN family + Protocol 2 support
is planned for v0.4.  v0.1-v0.3 stay HL2-only by scope, but the
following five disciplines apply during HL2 work to avoid an
expensive retrofit later.  Future Claude sessions: enforce these
on every PR.

1. **`nddc` is a runtime value, not a magic constant.**  Read
   from `radio.protocol.nddc`, never hard-code `4`.  P2 ANAN
   models have varying DDC counts (G2 = 4, 7000DLE = 7).  The
   abstraction is free if added now; expensive to retrofit.

2. **`Radio` facade is hardware-agnostic.**  Public methods
   accept logical units (Hz, dB, mode names).  Hardware-specific
   conversions (e.g. HL2's TX attenuator -28..+31 dB ↔ a generic
   "TX drive" range) live inside `lyra/protocol/p1_hl2.py` and
   eventually `p2_anan.py`.  Smell test: if a method name
   contains "hl2", it's in the wrong layer.

3. **Don't kill the sounddevice audio path permanently.**  §6.1
   says "no host-side sounddevice path for v0.0.9" — that's right
   for HL2 (AK4951 is canonical) but wrong as a permanent
   architectural choice.  ANAN audio comes back via P2 over
   Ethernet to the host; sounddevice (or sibling) renders it.
   `AudioSink` interface stays clean so re-adding sounddevice is
   one new file in `lyra/audio/`, not a refactor.

4. **PureSignal posture conditional on radio capabilities.**
   HL2 PS = hardware mod required (operator self-attestation
   per §6.5).  ANAN G2 PS = built into stock gateware.  v0.3
   should branch on `radio.capabilities.puresignal_requires_mod`,
   not hardcode the attestation checkbox into the UI.  The
   capabilities object is a per-radio-class struct populated in
   the protocol module.

5. **TX hardware quirks live in protocol module, not DSP.**  HL2:
   TX attn -28..+31 dB, CWX PTT bit at I-LSB bit 3.  ANAN G2: TX
   attn 0..31 dB, standard CWX bit positions.  None of this leaks
   into `lyra/dsp/tx_*.py` — DSP produces baseband I/Q at the
   rate the protocol layer asks for, full stop.  All hardware
   quirks belong in `lyra/protocol/p1_hl2.py` (today) and
   `lyra/protocol/p2_anan.py` (v0.4).

When v0.4 starts, the protocol module gets split:

```
lyra/protocol/
├── __init__.py
├── stream.py            # current — rename to p1.py + thin shim
├── p1.py                # NEW — HPSDR Protocol 1 base
├── p1_hl2.py            # NEW — HL2-specific quirks (mostly today's stream.py)
├── p2.py                # NEW v0.4 — HPSDR Protocol 2 base
├── p2_anan.py           # NEW v0.4 — ANAN-specific quirks
└── capabilities.py      # NEW v0.4 — radio-class capability struct
```

The §3 "HL2 protocol critical facts" reference stays under that
heading; v0.4 adds §3b "ANAN P2 critical facts."

## 7. Phased delivery roadmap

### v0.0.9 — Memory & Stations (SHIPPED 2026-05-02)

Pre-RX2 polish release.  TIME button (HF time-station cycle),
GEN1/2/3 customization, 20-slot Memory bank with CSV import/export,
EiBi shortwave broadcaster overlay with auto-detection.  See
`CHANGELOG.md` [0.0.9].

### v0.1 — RX2 (next)

- Phase 0: multi-channel refactor (no behavior change).
- Phase 1: protocol RX2 enablement (nddc=4, EP6 parser rewrite).
- Phase 2: stereo split audio routing.
- Phase 3: UI integration (focus model, hotkeys, A↔B/Swap/Lock buttons).
- Phase 4: split panadapter (vertical splitter in central widget).
- Phase 5: polish, persistence, docs.
- Rolling pre-releases per phase.

### v0.2 — TX (post-RX2)

- v0.2.0: SSB only (USB/LSB) + PTT + drive level + fwd/rev power.
- v0.2.1: CW (with internal keyer + sidetone, CWX PTT bit), AM,
  compressor port from WDSP.
- v0.2.2: FM, CFC.
- v0.2.3: Leveler, equalizer.

### v0.3 — PureSignal

- Port `calcc.c` + `iqc.c` + `xbuilder` + `delay.c`.
- New `PSDialog` UI modeled on Thetis's `PSForm.cs`.
- Auto-attenuator state machine (HL2-specific bounds).
- Coefficient persistence to `~/.config/lyra/ps_corrections/`.
- Operator self-attestation checkbox (HL2; ANAN G2 won't need it).

### v0.4 — Multi-radio refactor + Protocol 2 + ANAN (long-term)

Operator decision 2026-05-03: ANAN family support is a real
future direction.  Approach:

- v0.4.0: Protocol module split per §6.7 file layout (no
  behavior change for HL2 operators).  Capability struct
  populated for HL2; ANAN capability struct stubbed but inert.
- v0.4.1: Protocol 2 base implementation (`p2.py`) — discovery,
  framing, command structure.  Tested against an ANAN G2 unit.
- v0.4.2: ANAN-specific gateware quirks (`p2_anan.py`) — radio
  model detection, per-model DDC count, PS-without-attestation,
  TX attenuator range, audio routing via sounddevice (since ANAN
  has no AK4951 codec).
- v0.4.3: Settings UI — radio-model picker (auto-discover then
  select if multiple).  Documentation pass for ANAN operators.
- v0.4.4: Polish, second-radio testing on ANAN-7000DLE Mk2 (P1
  *or* P2 mode), older ANAN-100/200/8000 (P1-only — should
  already work via the HL2 path with minor capability
  differences).

The five hardware-abstraction disciplines in §6.7 govern PRs
during v0.1-v0.3 to keep this milestone tractable.  Without that
discipline, v0.4 becomes a six-month rewrite; with it, v0.4 is
a focused two-month push.

## 8. File path conventions

```
lyra/
├── __init__.py                    # version source of truth
├── radio.py                       # Radio class — channel dict + facades
├── protocol/
│   └── stream.py                  # HPSDR P1 — nddc=4, per-DDC freq, etc.
├── dsp/
│   ├── channel.py                 # per-RX DSP chain (existing)
│   ├── mix.py                     # NEW v0.0.9 — StereoMixer + WDSP pan curve
│   ├── tx_channel.py              # NEW v0.1 — TX DSP chain
│   ├── ssb_mod.py                 # NEW v0.1 — SSB modulator
│   ├── cw_keyer.py                # NEW v0.1.1
│   ├── tx_compressor.py           # NEW v0.1.1 — port from compress.c
│   ├── ps_calcc.py                # NEW v0.2 — port from calcc.c
│   ├── ps_iqc.py                  # NEW v0.2 — port from iqc.c
│   ├── ps_xbuilder.py             # NEW v0.2 — cubic-spline coef builder
│   └── delay_line.py              # NEW v0.2
├── radio/
│   └── ptt.py                     # NEW v0.1 — PTT state machine
├── ui/
│   ├── panels.py                  # extend for RX2/TX/PS controls
│   ├── spectrum.py                # add split-vertical mode for dual pan
│   └── ps_dialog.py               # NEW v0.2 — modeled on PSForm.cs

docs/architecture/                  # research + plans (this conversation)
├── implementation_playbook.md     # AUTHORITATIVE — start here
├── v0.0.9_rx2_plan.md
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
    ├── compress.c                 # TX compressor (port for v0.1.1)
    ├── lmath.c                    # xbuilder cubic-spline (port for v0.2)
    ├── delay.c                    # delay line (port for v0.2)
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

## 9.7. Click-to-tune v1 — partially shipped, needs refinement

Shipped across v0.0.7.1 → v0.0.7.4:
- Plain click → literal tune (always worked, unchanged from v0.0.7).
- Click+drag → drag-to-pan (rate-limited to ~30 Hz emit cadence
  to avoid backend-pipeline overload).  Working OK per operator
  flight test.
- Shift+click → snap to nearest spectrum peak.  Reticle preview
  on hover.  **Operator verdict (2026-05-02): "a little better —
  needs work."**

What got fixed across the four patch tags:
- `v0.0.7.2` 7b1c79c... `v0.0.7.4`: GPU widget had no drag state
  machine (committed click on press), no `setMouseTracking(True)`
  (hover never fired), and the snap range was a fixed 200 Hz
  (only ~3 px wide at typical wide zoom).  Plus snap target was
  recomputed on RELEASE checking `event.modifiers()` -- if the
  operator released Shift before the mouse the click fell through
  to literal-tune.
- v0.0.7.4 final fix: latch snap target at PRESS time so the
  commit is atomic w.r.t. Shift state.

Known refinement candidates (parked for next session):
1. **"Click misses" residual.**  After the press-time-latch fix,
   operator still reports "a little better, needs work."  Specific
   symptom not yet collected -- possibilities:
   - Parabolic peak interpolation might be off by a few Hz at
     wide zoom (FFT bin width ~47 Hz at 192 kHz / 4096 bins).
     The interpolation gives sub-bin precision but bins are
     finite-width to begin with.
   - Reticle might be drawn at a slightly different position
     from where the snap commits.  The reticle position uses
     the CURRENT span/center (live as you hover) but the snap
     target is a frequency captured at press time -- if span
     changes between hover and press, the visual position can
     drift.
   - Snap might find sidelobes or artifacts instead of true
     peak center.  The argmax inside the search window is the
     local maximum but doesn't validate it's a "real" signal
     vs a noise blip or filter ringing.
2. **Snap range could be smarter.**  Current effective range is
   `max(snap_tune_range_hz=200, SNAP_PIXEL_RADIUS=80 * hz_per_px)`.
   At 192 kHz / 1500 px that's 10240 Hz -- might be too wide
   (snaps to a stronger nearby signal instead of the one
   operator pointed at).  Could cap the pixel radius at e.g.
   3000 Hz to keep snap "closest peak you pointed at" rather
   than "anything strong nearby."
3. **Snap might benefit from a stronger SNR test.**  Current
   threshold is `peak_db - noise_floor_db >= 6 dB`.  Noise floor
   is the 20th percentile of the spectrum, so 6 dB above that is
   a low bar -- weak ambient peaks can pass.  Could raise to
   10 dB or use median + N*MAD instead.
4. **Reticle could drag-track better.**  Currently updates every
   mouseMoveEvent that has Shift held -- which works, but at
   wide zoom tiny cursor jitter can flicker the reticle between
   adjacent peaks.  Could add a small position-stability hold
   so the reticle doesn't twitch.
5. **Settings → Spectrum tab.**  No operator-facing controls for
   snap range / SNR threshold / modifier choice / reticle
   visibility.  Defaults are baked in.  Once the algorithm feels
   right, expose the knobs.

Operator-facing UX is documented in `docs/help/spectrum.md`
("Click-to-tune" section) and `docs/architecture/click_to_tune_plan.md`
(design proposal).

When circling back: ask operator what specifically still feels
wrong (which test case fails -- weak signal? wide zoom? CW
sidelobe pickup?) before tweaking the algorithm.  Each candidate
above has a different fix.

## 9.8. Speaker-selective audio attenuator — WITHDRAWN 2026-05-10

Operator removed from the backlog 2026-05-10: post-WDSP audio
chain (NR Mode 1-4 + AEPF + NPE + ANF + NB + APF + per-band
SQ) handles the original use cases well enough that the
"selectively attenuate one voice in a roundtable" feature is no
longer needed.  Section retained below as historical record so
anyone reading old docs that reference §9.8 can find context,
but no implementation work expected.

**Original entry preserved below, marked WITHDRAWN.**

**Operator-suggested 2026-05-02:** in a roundtable QSO, attenuate
ONE specific operator's voice while keeping the others audible.
Use case: leave the radio on while away from the desk, want
to hear the conversation but skip the operator you don't enjoy.

**Captured-noise-profile feature WILL NOT do this.**  Spectral
subtraction works for stationary noise; voices are non-stationary
and share broad spectral characteristics across speakers.
Subtracting "Bob's voice profile" produces a generic voice-band
EQ cut applied to ALL voices, not Bob-specific suppression.
This is a math constraint, not a tuning issue.

**The right architecture (if we build it):**

- VAD-gated **per-turn classification** — detect voice onset, run
  classifier once 750 ms into the turn, latch decision for the
  rest of the turn (resets on detected silence).
- **Probabilistic attenuator** -- output is
  ``attenuation = score × max_atten_db`` smooth-ramped.  Operator
  hears unwanted voice fading from full level to ~-15 dB; doesn't
  go to silence.  False positives produce mild ducking instead of
  catastrophic Alice-was-muted-during-her-turn outcomes -- crucial
  for the "operator walks away" use case.
- **Multi-profile** -- match against a list of "skip these voices"
  (roundtables often have 2-3 operators to skip).
- **"Tag this voice" hotkey** -- operator hears Bob, presses key,
  current 5-10 sec captured as profile, auto-engaged.  No prep
  required.

**Two scorer options:**

1. Pure NumPy multi-feature (LTAS + F0 stats + spectral tilt +
   formants + speaking rate).  ~1 week dev.  70-85% accuracy on
   same-gender / similar-mic speakers.  Borderline for
   "leave-the-radio-on" reliability.
2. Pretrained ONNX speaker embedding (ECAPA-TDNN-class).  ~2-3
   weeks dev including license / bundling.  90-98% accuracy.
   Adds onnxruntime dep + ~10 MB ONNX model in assets/.
   Recommended for unattended-listening use case.

**Why we're NOT building this now:**

- Not a v0.0.x quiet-pass tweak; substantial new feature surface
  (capture UX, profile management, scoring engine, attenuator
  gate).
- Right after RX2 (v0.0.9) and TX (v0.1) is the natural slot
  -- earlier and we're spreading bandwidth too thin.
- Unattended-listening reliability bar is high enough that
  Approach 2 (ML) is the right target, which means the licensing
  + ONNX dep work is a hard prerequisite.
- Operator categorized this as "niche, might be interesting"
  rather than blocking pain.

**Status:** parked.  No design doc written yet.  When circling
back, write `docs/architecture/speaker_filter_design.md` first
(the probabilistic-attenuator math, ECAPA-TDNN evaluation
checklist, license review for permissive ONNX models, capture +
profile UX flows).  THEN implement on a feature branch alongside
v0.2 work or as a v0.2.x post-PureSignal feature.

Pre-park reasoning lives in this conversation thread (operator
asked, I gave the senior-engineering analysis, operator parked).
If reading this in a future session, the analysis was: spectral
subtraction is the wrong tool, per-turn classification with
probabilistic attenuation is the right tool, ECAPA-TDNN-class
embeddings are the right scorer, ham radio's stable-mic property
makes accuracy slightly better than general-voice benchmarks
suggest.

## 10. Open empirical questions (need HL2+ bench testing)

These weren't answered by code-reading; we'll find out on N8SDR's
hardware:

1. **HL2 mic samples in EP6 with AK4951 audio active** — value or
   zero?  Affects v0.1 mic-input source choice.
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

Numbered steps so nothing slips through the cracks — this list
exists because the v0.0.9.6 through v0.0.9.9 releases all
skipped step 8 (push to main), leaving anyone tracking
``origin/main`` pulling v0.0.9.5 code while four feature releases
piled up on the feature branch.

1. **Bump version** in two places: `lyra/__init__.py`
   (`__version__`, `__version_name__`, and flip `__build_date__`
   from ``"dev"`` to today's `YYYY-MM-DD`) and
   `build/installer.iss` (`LyraVersion`, `LyraVersionName`,
   `LyraBuildDate`).
2. **Update `CHANGELOG.md`** — new dated entry at the top
   (consolidated; replaces per-version RELEASE_NOTES files).
3. **Update `CLAUDE.md`** version-numbering history near the top
   of the file.
4. **Commit** the version bump.
5. **Annotated tag** with release notes:
   `git tag -a v0.0.X -m "..."`.
6. **Build** via `build/build.cmd` (PyInstaller + Inno Setup).
   Verify installer lands at `dist/installer/Lyra-Setup-X.Y.Z.exe`.
7. **Push feature branch + tag**:
   `git push origin <feature-branch>` then
   `git push origin v0.0.X`.
8. **Push to main** (the step that was missing from v0.0.9.6
   through v0.0.9.9): `git push origin <feature-branch>:main`,
   which fast-forwards `origin/main` to the release commit
   without needing a local main checkout.  If you skip this,
   GitHub's web UI shows the release correctly but
   `git pull origin main` returns stale code — anyone tracking
   main is reading v0.0.9.5 while installers up through v0.0.9.9
   ship.
9. **Create GitHub Release** manually (or via `gh release create`
   if the CLI is installed): tag = `v0.0.X`, title = `v0.0.X —
   <Version Name>`, body = release notes, attach the
   `Lyra-Setup-X.Y.Z.exe` from `dist/installer/`.

``build/build.cmd`` prints a reminder of this sequence after the
build completes — if a step is missed, the cmd-window output is
the place to spot it.

### Pre-releases for tester feedback

- Cut pre-releases per phase during long features (worked well for
  v0.0.6 / v0.0.7).
- v0.0.9 phases: 0 (refactor), 1 (protocol), 2 (audio), 3 (UI),
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
and I'll know what's in scope.  For example: "Start v0.0.9 Phase 0"
means multi-channel refactor with no behavior change.

When something I do conflicts with this doc, **trust this doc over
my session memory** — this is the consolidated source of truth.  If
this doc is wrong, we update it explicitly.

---

## 13. Audio architecture (locked 2026-05-06)

After multiple deep dives that kept circling, an operator review
of the Thetis source tree + Thetis settings database produced the
canonical answer.  See `docs/architecture/audio_architecture.md`
for the full reasoning trail; below is the operative summary.

### 13.1 The two audio paths

**Path A — HL2 onboard codec via EP2 (DEFAULT for HL2 hardware).**

```
HL2 IQ  →  Lyra DSP chain  →  L/R audio in EP2 frames  →  back to HL2  →  onboard codec  →  headphone jack
```

This is the path Thetis defaults to for HermesLite hardware
(`audioCodecId = HERMES`, `cmsetup.c:75`).  Single crystal (the
HL2's), zero clock drift, no resampler needed.  Lyra has called
this "AK4951 mode" through v0.0.9.5; **v0.0.9.6 renames it to
"HL2 audio jack"** since not all HL2 revisions use the AK4951
specifically but all use the same EP2 codec path.

**Path B — Host PC sound card via SoundDeviceSink.**

```
HL2 IQ  →  Lyra DSP chain  →  WDSP rmatch (PI loop) → varsamp →  ring buffer  →  WASAPI/PortAudio  →  PC speakers
```

Required for:
- HL2 operators who can't or don't want to use the codec path
- ANAN family (v0.4) which has no onboard codec at all
- Audio routing to other apps (digital mode software, recording)

### 13.2 Why two paths

- Thetis's primary audio path (HermesLite) is HERMES-only.  It
  doesn't even *implement* WASAPI for output (`netInterface.c:
  1757-1759 — case WASAPI: // not implemented`).  Thetis's
  PC-soundcard support is ASIO via `cmasio.c`, which uses the
  same rmatch/varsamp adaptive resampler chain that Path B
  needs.
- The HL2 onboard codec path is single-crystal, so there's no
  rate mismatch to compensate for.  Operators who can use it
  get glitch-free audio for free, no DSP overhead.
- The PC sound card path has fundamental two-clock drift
  (HL2 crystal vs sound card crystal, both nominally 48 kHz,
  both ±50 ppm tolerance).  Without an adaptive resampler the
  ring buffer fills (overrun) or drains (underrun) over time.
  This is what produced operator-reported audio glitches in
  Lyra v0.0.9.x PC Soundcard mode.

### 13.3 The WDSP-port-not-Thetis-copy principle (restated)

Lyra is GPL v3+, WDSP is GPL v3+.  License-compatible.  WDSP
is its own GPL'd DSP project that Thetis happens to use; Lyra
ports directly from WDSP with attribution.  **This is not
"ripping from Thetis."**  Same pattern as `agc_wdsp.py` (port
of `wcpAGC.c`), `nr.py` (`anr.c`/`emnr.c`), `nr2.py` (`emnr.c`),
`lms.py`, `anf.py`, `nb.py` — all already shipped.

What we DO port (with attribution comment per
`docs/architecture/wdsp_integration.md`):

| When | WDSP file | Lyra target | LOC | Unblocks |
|---|---|---|---|---|
| **v0.0.9.6** | `aamix.c` | `lyra/dsp/mix.py` | ~200 | RX1+RX2 mix routing |
| **v0.0.9.6** | `varsamp.c` | `lyra/dsp/varsamp.py` | ~400 | PC sound card drift, ANAN audio |
| **v0.0.9.6** | `rmatch.c` | `lyra/dsp/rmatch.py` | ~700 | PI control loop on top of varsamp |
| **v0.0.9.6** | `patchpanel.c::SetRXAPanelPan` | `lyra/dsp/mix.py` | ~50 | RX2 stereo pan curve |
| v0.2 | `compress.c` | `lyra/dsp/tx_compressor.py` | ~150 | TX compressor |
| v0.2 | `eqp.c` | `lyra/dsp/eq.py` | ~300 | Parametric EQ (RX + TX) |
| v0.2 | `delay.c` | `lyra/dsp/delay_line.py` | ~80 | TX delay matching, PS feedback |
| v0.3 | `iqc.c` | `lyra/dsp/ps_iqc.py` | ~315 | PS predistortion application |
| v0.3 | `calcc.c` | `lyra/dsp/ps_calcc.py` | ~1164 | PS calibration math |
| v0.3 | `lmath.c::xbuilder` | `lyra/dsp/ps_xbuilder.py` | ~200 | Cubic-spline PS coefficient |

What we DO NOT copy (these are Thetis-specific glue, not WDSP
algorithms):

- `Console/console.cs` — study `UpdateDDCs` etc. as reference,
  write Lyra-native equivalents.
- `Console/PSForm.cs` — study the state machine, write Lyra-
  native (`lyra/ui/ps_dialog.py`).
- `ChannelMaster/networkproto1.c`, `cmaster.c`, `network.h` —
  study the protocol bit layouts in CLAUDE.md §3, write Lyra-
  native (`lyra/protocol/stream.py`).
- `Console/HPSDR/IoBoardHl2.cs` — study HL2 I/O quirks, write
  Lyra-native.

What we DO NOT port from WDSP because Python+NumPy+Qt does it
natively or differently:

- `analyzer.c` — Lyra has its own GPU spectrum widget.
- `channel.c` — buffer mgmt; GIL handles it.
- `main.c` — Win32 thread mgmt; Python threading.
- `RXA.c`/`TXA.c` — channel scaffolding; Lyra has its own.

### 13.4 Hardware capability struct (extends §6.7)

The hardware-abstraction discipline in §6.7 needs an audio
field added when v0.4 work begins:

```python
@dataclass
class RadioCapabilities:
    nddc: int                        # advertised DDC count
    has_onboard_codec: bool          # HL2 = True, ANAN = False
    default_audio_path: AudioPath    # HL2 = HL2_CODEC, ANAN = PC_SOUND
    puresignal_requires_mod: bool    # HL2 = True, ANAN G2 = False
    tx_attenuator_range: tuple[int, int]   # HL2 = (-28, 31), ANAN = (0, 31)
    cwx_ptt_bit_position: int        # HL2 = 3, ANAN = standard
    # ...
```

When Lyra opens a connection, the protocol module populates
this struct.  UI defaults read from it.  Settings UI lets the
operator override per-radio (e.g., HL2 operator who prefers PC
sound card despite having a codec).

### 13.5 What this changes about RX2 / TX / PureSignal plans

- **RX2 (v0.1):** No change.  Stereo split via EP2 LR bytes
  through HL2 codec, exactly as planned in §6.1.  The
  `aamix.c` port for v0.0.9.6 is the prerequisite that makes
  RX2 work when it lands.
- **TX (v0.2):** No change.  Default mic input is HL2 mic jack
  via EP6 (single crystal, no drift).  PC mic becomes opt-in
  for ANAN-class hardware in v0.4 — that path uses the same
  rmatch+varsamp from v0.0.9.6 for input-side rate matching.
- **PureSignal (v0.3):** No change.  HL2 PS feedback is on
  DDC2/DDC3 at `rx1_rate` per §3.8 — single crystal, no drift.
  Different DDC rates (e.g., ANAN's 192 kHz `ps_rate` vs the
  user-selected RX rate) is a rate-conversion problem solved
  by the v0.0.9.6 varsamp port.

The audio infrastructure ships once (v0.0.9.6) and gets used
three more times (RX2 stereo, TX mic input on ANAN, PS rate
conversion).

---

## 14. WDSP-DLL integration architecture (added 2026-05-06)

The audio-quality work in v0.0.9.6 pivoted from "port WDSP modules
into Python" to "call into the WDSP DSP engine via cffi."  This
section is the operative reference for how that's wired.

### 14.1 Files that matter

| File | Role |
| --- | --- |
| `lyra/dsp/_native/` | Bundled native DLLs (~16 MB total): `wdsp.dll`, `libfftw3-3.dll`, `libfftw3f-3.dll`, `rnnoise.dll`, `specbleach.dll` |
| `lyra/dsp/wdsp_native.py` | cffi cdef declarations + DLL loader. Search order: explicit `dll_dir` arg → `LYRA_WDSP_DIR` env var → bundled `_native/` → fallback Thetis-HL2 install dirs (dev convenience only). |
| `lyra/dsp/wdsp_engine.py` | High-level Python wrapper: `RxChannel`, `RxConfig`, `RxaMode`, `AgcMode`, `MeterType`. Stable API surface for Radio. |
| `lyra/radio.py` `_open_wdsp_rx`, `_do_demod_wdsp`, `_wdsp_*_for` helpers | Integration into Radio. Default ON; `LYRA_USE_LEGACY_DSP=1` falls back. |
| `lyra/dsp/worker.py` `process_block` | Worker-mode dispatch into `_do_demod_wdsp` + still calls `_maybe_run_fft` so panadapter is fed. |
| `scratch/wdsp_port_status.md` | Living status doc. |
| `scratch/test_wdsp_poc.py` | Standalone PoC. Run to verify the engine path is healthy without launching the full app. |

### 14.2 What's wired vs what's pending

**Wired (works in WDSP mode):**
- RX1 audio: IQ in → WDSP RXA → 48 kHz stereo audio → audio sink
- Mode: USB / LSB / AM / FM / CWU / CWL / DSB / SAM / DIGU / DIGL / DRM / SPEC
- RX bandwidth (per-mode, propagates filter freqs to NBP0 + BP1)
- Rate change (closes + reopens WDSP channel at new in_rate)
- AGC mode + the operator picker (Off / Fast / Med / Slow / Auto /
  Custom) via SetRXAAGCMode.  ``"long"`` is fully wired in
  ``radio.py`` but currently NOT exposed in the ``_AGC_PROFILES``
  right-click menu — see §15.5 to re-add (one-line change in
  ``panels.py``).  Auto profile additionally runs
  ``auto_set_agc_threshold`` on a 1-sec timer to re-calibrate
  ~18 dB above the rolling noise floor.
- AGC gain readout (GetRXAMeter / RXA_AGC_GAIN, throttled to ~6 Hz)
- AGC threshold + AF gain wiring (SetRXAAGCThresh + WDSP PanelGain1
  per Phase 6.A1/A3 fixes during the v0.0.9.6 cleanup arc)
- **NR-mode UX**: 4-position picker (Mode 1 / 2 / 3 / 4) mapping
  to WDSP gain methods 0..3 (Wiener+SPP / Wiener simple / MMSE-LSA
  default / Trained adaptive) + AEPF anti-musical post-filter
  + NPE method picker (OSMS / MCRA / etc.).  See §14.7.
- ANF (auto-notch) — profile picker + μ slider mapped to
  ``SetRXAANFVals`` (Phase 6.A4).
- LMS (independent toggle, μ slider drives WDSP ANR step size).
- All-mode squelch via WDSP SSQL (SSB/CW/DIG/SPEC), FMSQ (FM),
  AMSQ (AM/SAM/DSB) — see §14.8.  Threshold sliders mapped
  per-module.
- Manual notches (right-click on spectrum) — wired via
  ``RXANBPAddNotch`` / ``DeleteNotch`` / ``SetNotchesRun`` /
  ``SetTuneFrequency`` (Phase 6.A4).
- NB (noise blanker) — ``create_nob`` / ``create_anb`` initialized
  in ``RxChannel.__init__``; profile picker drives NOB threshold
  via ``_push_wdsp_nb_state`` (xnobEXT / xanbEXT splice into the
  IQ path).
- Binaural (BIN) Hilbert phase split — runs as Python post-
  processor on WDSP's stereo output, both HL2-jack and
  PC-Soundcard paths.
- APF (CW peaking, mode-gated to CWU/CWL) via WDSP SetRXABiQuad
  SPEAK biquad — center freq tracks ``cw_pitch_hz`` in audio
  domain.
- CW pitch (refilters BP1 + NBP0 + SNBA collectively via
  RXASetPassband when active mode is CWU/CWL; under v0.0.9.8's
  carrier-freq VFO convention also re-pushes the DDS-vs-VFO
  offset so the operator's tuned carrier stays inside the
  passband at the new pitch).
- Volume + mute (applied in Python after WDSP).
- TCI audio tap (applied in Python after WDSP).
- TPDF dither on float→int16 quantization for HL2 audio jack.
- S-meter peak-hold smoothing (~500 ms decay) — Python-side
  fast-attack / slow-release on the FFT-derived meter.
- Spectrum / panadapter / waterfall + per-band bounds memory
  (incl. waterfall min/max as of v0.0.9.7) + carrier-freq VFO
  convention with central DDS offset (v0.0.9.8 — see §15.6
  trailer / version-numbering history).
- Captured noise profile capture + apply (v0.0.9.9 §14.6 Phase 4
  IQ-domain rebuild) — both halves run pre-WDSP via
  ``CapturedProfileIQ`` (``lyra/dsp/captured_profile_iq.py``);
  v2 schema with rate-specific full complex-FFT magnitudes
  (``lyra/dsp/noise_profile_store.py`` SCHEMA_VERSION = 2);
  v1 audio-domain profiles refused on load with recapture hint;
  cross-rate / cross-FFT-size profiles refused with
  operator-friendly errors.

**Inert in WDSP mode (deferred):**
- NR3 (RNNoise) and NR4 (Spectral Bleach).  ``rnnoise.dll`` and
  ``specbleach.dll`` are bundled but no operator UI is wired
  yet.  Adding a fifth and sixth NR mode to the picker is a
  small task once a tester asks for it.
- Audio Leveler — DELETED in the v0.0.9.6 cleanup arc (Phase 4).
  WDSP AGC subsumed its dynamic-range function; the
  ``lyra/dsp/leveler.py`` source is gone.  RX2 plan §7.x still
  references it at a few spots — see §15.2 backlog item.
- TX (Phase v0.2) and PureSignal (Phase v0.3) — entire chains
  are out of scope for the v0.0.9.x line; first TX work begins
  with v0.1 RX2 finished.

**Crucial gotcha — WDSP filter convention:**
WDSP's USB filter at `(+200, +3100)` selects content from the
**negative** baseband, and LSB filter at `(-3100, -200)` selects
**positive** baseband. (Internal NCO/demod sign flip in WDSP.)
HL2 baseband is mirrored: USB-RF lands at negative baseband, LSB
at positive. The two flips cancel out. So we hand HL2 IQ to WDSP
**unmodified** and get correct sideband selection — the same way
Thetis does. An earlier `np.conjugate(iq)` "compensation" was
WRONG and produced reversed sidebands; do not re-add it without
re-verifying with the synthetic-tone PoC.

**Crucial gotcha — bandpass dispatch:**
WDSP has TWO bandpass filters in the RXA chain. `BP1`
(`SetRXABandpassFreqs`) is post-NR and only RUNS when AM/SAM/EMNR/
ANR/ANF/SNBA is on. `NBP0` (`RXANBPSetFreqs`) is front-of-chain
and always runs. SSB sideband selection lives in NBP0. The
`RXASetPassband` collective updates BOTH (plus the SNBA output
filter) and is what we call from `RxChannel.set_filter()`.
**Do not** call `SetRXABandpassFreqs` directly for sideband
selection — with all DSP off, BP1 is bypassed and the call is
silently ignored.

**Crucial gotcha — OpenChannel "block" parameter:**
The 13th parameter to `OpenChannel` is a "block until output
available" flag, not a CW BFO offset. Pass 1, not 0. The WDSP
source comment is `// block until output available`. Passing 0
makes `fexchange0` non-blocking and the output buffer can return
stale data.

**Crucial gotcha — output buffer size:**
`fexchange0` writes `out_size = in_size * out_rate / in_rate`
frames, NOT `in_size` frames. With in_size=1024 IQ at 192 kHz
and out_rate=48 kHz, the output buffer holds 256 frames of audio,
not 1024. Allocating 1024 leaves uninitialized memory in the
trailing 768 slots and produces a buzzing "electrocuted" sound
at the block rate. `RxChannel.__init__` computes `out_size`
correctly; don't override it.

### 14.3 Threading model with WDSP

Same as §5 except the per-RX DSP heavy work moves into WDSP's
own internal thread (created by `_beginthreadex` inside the DLL,
not visible to Python). The Python worker thread (B.x changes)
still runs `process_block` per IQ batch but the actual DSP
arithmetic is GIL-free C now. That's the architectural fix that
ended the click / motorboat saga: Python's writer / sink threads
no longer compete with the DSP for the GIL.

### 14.4 Deferred / open work — RX1 polish push 2026-05-07 status

**Items done this session (2026-05-07 RX1 polish):**

1. ~~**PC Soundcard CPU optimization.**~~ ✓ DONE — `WdspRMatch`
   class in `lyra/dsp/rmatch.py` cffi-wraps the bundled DLL's
   `xrmatchIN`/`xrmatchOUT`.  `SoundDeviceSink` picks it
   automatically when WDSP loads, falls back to pure-Python
   `RMatch` otherwise.  Operator-confirmed CPU very close to
   HL2-jack mode.

2. ~~**NB (noise blanker) wiring.**~~ ✓ DONE — `create_nob`/
   `create_anb` cffi bindings added; `RxChannel.init_blankers`
   runs in `__init__`; `xnobEXT`/`xanbEXT` actually splice into
   the IQ path before `fexchange0` (the `SetEXTNOBRun(1)` flag
   alone is just a marker).  Profile mapping (off/light/medium/
   heavy/custom) drives NOB threshold via `_push_wdsp_nb_state`.

3. ~~**Manual notches.**~~ ✓ DONE — `RXANBPAddNotch` /
   `RXANBPDeleteNotch` / `RXANBPSetNotchesRun` /
   `RXANBPSetTuneFrequency` wired through
   `RxChannel.set_notches` / `set_notches_master_run` /
   `set_notch_tune_frequency`, hooked into `notches_changed`
   signal in radio.py.

4. **Captured noise profile + APF + Leveler + BIN — split decisions.**
   - **APF** ✓ WIRED via WDSP `SetRXABiQuad*` (the SPEAK biquad).
     Mode-gated to CWU/CWL.  Operator-confirmed "+12 dB measured
     at +12.2 dB" working.
   - **BIN** ✓ WIRED as Python post-processing on WDSP's stereo
     output, in BOTH HL2 audio jack and PC Sound paths.  PC Sound
     required complex-rmatch routing (L into I, R into Q) so
     channels survive rate-matching independent.
   - **Leveler** ✓ DROPPED — WDSP AGC subsumes it.
   - **Captured noise profile** ✓ **WIRED — IQ-domain (v0.0.9.9
     §14.6 Phase 4)**.  Both capture and apply run pre-WDSP on
     raw IQ; the operator-driven "use captured" toggle now
     enables real spectral subtraction at the IQ layer.  Operator
     hears noise floor drop ~6-12 dB.  Three earlier post-WDSP
     audio-domain attempts in v0.0.9.6 produced AGC-mismatch
     artifacts and were reverted; the IQ-domain rebuild
     sidesteps that interaction (see §14.6 below for the full
     trail).

5. **Cleanup pass.** Once RX/TX/PS are all on the native engine,
   audit `lyra/dsp/agc_wdsp.py`, `nr.py`, `anf.py`, `lms.py`,
   `demod.py`, `channel.py`, `leveler.py`, `apf.py` for what's
   still doing real work vs dead code reachable only via
   `LYRA_USE_LEGACY_DSP=1`.  Modules to KEEP regardless:
   `wdsp_native.py`, `wdsp_engine.py`, `audio_sink.py`,
   `audio_mixer.py`, `binaural.py`, `rmatch.py`, `varsamp.py`,
   `noise_profile_store.py`, `nr2.py` (used by capture path),
   `worker.py`, `mix.py`, `squelch.py`.  See
   `docs/architecture/measurements_and_cleanup.md` for the
   four-phase plan.

**Items still pending — not started:**

6. **TPDF dither on HL2 audio quantization.** ✓ DONE 2026-05-07
   — `_quantize_to_int16_be` in `lyra/protocol/stream.py`.
   Operator-confirmed harshness gone.

7. **S-meter peak-hold smoothing.** ✓ DONE 2026-05-07 —
   fast-attack/slow-release with ~500 ms decay constant.
   Operator-tunable via `_SMETER_PEAK_DECAY`.

8. **WDSP-native S-meter switch.** Bigger structural fix per
   Thetis A/B research: drop the FFT-derived meter, use
   `_wdsp_rx.get_meter(MeterType.S_PK) + cal + LNA`.  Cal trim
   would drop from ~+28 dB → ~+1 dB (Thetis HL2 default 0.98).
   Operator's manual cal of 59.5 dB to match Thetis on WWV is
   working well enough that this is now optional.  Documented
   in `docs/architecture/measurements_and_cleanup.md`.

### 14.4.1 Hot points to investigate when picking back up

* See §14.6 for the captured-profile-apply known issue + the
  IQ-domain architectural plan (NEW — replaces the failed
  post-WDSP audio-domain attempts).
* See §14.7 for the NR-mode UX overhaul status (in operator
  testing as of 2026-05-07 evening).
* RX2 work (v0.1) needs the audio-mixer plumbing already in
  `mix.py` to be exercised — we built the foundation but haven't
  driven a second WDSP channel through it.
* TX (v0.2) will need a sibling `wdsp_tx_engine.py` modeled on
  `wdsp_engine.py`, plus the protocol-layer power scaling per
  `docs/architecture/measurements_and_cleanup.md` §2.2.

### 14.5 Where to look when something's off

* **Engine won't load** — DLL set missing or wrong arch. Check
  `lyra/dsp/_native/`. Confirm five files: `wdsp.dll`,
  `libfftw3-3.dll`, `libfftw3f-3.dll`, `rnnoise.dll`,
  `specbleach.dll`. cffi error message names the missing DLL.
* **Audio is silent** — `LYRA_USE_LEGACY_DSP` set inadvertently?
  Check `Radio._use_wdsp_engine` is True. Then check
  `_wdsp_rx is not None`.
* **USB and LSB swapped** — someone re-added the conjugation.
  Don't.
* **Panadapter is dead but audio works** — worker mode bypassed
  the FFT stage. `worker.py` `process_block`'s WDSP branch must
  fall through to `_maybe_run_fft(samples)` before returning.
* **Buzzing tone, no usable audio** — output buffer size wrong.
  Confirm `RxChannel.out_size` matches `in_size * out_rate /
  in_rate` (when in_rate ≥ out_rate).

### 14.6 Captured-profile IQ-domain rebuild (v0.0.9.9)

**Status as of v0.0.9.9 Phase 4 (2026-05-10):** the IQ-domain
rebuild is **LIVE in WDSP mode**.  Capture taps raw IQ pre-WDSP
(``Radio._do_demod_wdsp`` → ``CapturedProfileIQ.accumulate``),
apply runs Wiener-from-profile spectral subtraction on raw IQ
also pre-WDSP (``CapturedProfileIQ.apply``), the cleaned IQ goes
to ``_wdsp_rx.process``.  Operator-perceptible noise reduction
~6-12 dB depending on band conditions and mask floor (default
-12 dB).  Phase 5 still pending: Settings → DSP FFT-size dropdown
(1024/2048/4096) and DSP+Audio panel badge polish for the v2
metadata.

**Schema:** profiles are v2 (``noise_profile_store.SCHEMA_VERSION
= 2``), domain ``"iq"``, full complex-FFT magnitudes (``fft_size``
floats), with per-profile ``rate_hz`` field.  v1 audio-domain
profiles from before v0.0.9.6's WDSP cleanup arc are refused on
load with a clear "recapture in v0.0.9.9+" hint
(``noise_profile_store.load_profile``).

**Historical context — what this rebuild replaced** (preserved
below for reference; the post-WDSP audio-domain path described
here is gone):

In WDSP mode the operator could capture noise profiles
(Cap button worked, profiles saved / loaded / persisted), but
enabling "use captured profile" did NOT apply spectral subtraction
to the audio.  Capture half worked, apply half didn't.  Operator
saw a status-bar warning at the moment of toggle.

**What we tried (2026-05-07 evening):**
1. First pass — wired `nr2.process()` as a Python post-WDSP audio
   stage, gated on `is_using_captured_source()`.  Operator reported
   crackle / pop on voice content.
2. Added temporal smoothing on the Wiener-from-profile gain mask
   (gated on the existing `musical_noise_smoothing` toggle).
   Modest improvement; operator still heard artifacts.
3. Added auto-VAD (`speech_aware = True`) for the WDSP captured-
   profile path.  Per-block flip caused UI readback inversion
   (NR1/NR2 labels swapping with VAD/captured), and operator still
   heard a steady tick + tonal drift even with all NR backends off
   — proving the artifacts are structural in the path, not parameter-
   tunable.

**Why fixes didn't stick:**

WDSP's AGC operates inside `fexchange0`.  Audio coming out of WDSP
is post-AGC, with dynamic levels driven by AGC's gain loop.  When
we apply spectral subtraction on top of that audio using the
captured profile (which represents noise levels at capture time),
the captured noise reference is mismatched against the live audio's
AGC-modulated noise floor.  The Wiener-from-profile gain math
swings rapidly per FFT frame in response — that's the tick.  No
amount of post-processing smoothing fully fixes it because the
underlying mismatch is between a static captured reference and a
dynamic live noise floor.

**The right architecture (operator-confirmed direction
2026-05-07 evening):** feed the captured profile into the IQ
chain BEFORE WDSP's AGC, NOT as a post-WDSP audio-domain pass.
Specifically — pre-WDSP IQ-domain spectral subtraction:

* **Capture path:** at capture time, FFT raw IQ blocks (192k or
  whatever rate is active), accumulate per-bin magnitudes, store
  as the captured profile.  This captures the IQ baseband noise,
  NOT audio-domain noise.
* **Apply path:** at runtime, FFT each IQ block in `_do_demod_wdsp`
  (or before WDSP's `process()`), subtract the captured profile in
  IQ-magnitude domain via Wiener-from-profile gain, IFFT back to
  IQ time domain, then hand the cleaned IQ to `fexchange0`.  This
  happens BEFORE WDSP's AGC and demod — sidesteps the AGC-mismatch
  that broke the post-WDSP audio-domain attempts.
* **Bonus:** IQ-domain captures are MODE-INDEPENDENT (same profile
  works for SSB/CW/AM/FM since the baseband noise pattern is the
  same regardless of demod choice).
* **Cost:** profiles become RATE-SPECIFIC (192k vs 96k vs 48k all
  need their own profile, since baseband bin structure differs).
  Profile storage needs a rate field.  `noise_profile_store.py`
  schema-bump.
* **Implementation cost:** ~2-3 days of focused work:
  1. New capture flow tapping IQ pre-WDSP instead of audio post-WDSP
  2. Apply flow with proper STFT overlap-add to avoid block-boundary
     artifacts
  3. Profile storage update for rate-specificity
  4. Testing across modes / rates / bands

**Operator's preference (2026-05-07 evening):** keep the captured-
profile feature alive in WDSP mode — it's a Lyra niche they value.
Park the apply path until we can do IQ-domain properly.  In the
meantime:

* Cap button still records data + saves profiles to QSettings.
* Profile manager still loads/lists them.
* Use-captured-profile toggle fires status-bar warning in WDSP mode.
* In legacy mode (`LYRA_USE_LEGACY_DSP=1`), captured-profile applies
  normally as before.

**Other paths previously considered (not preferred):**

1. Patch WDSP to expose `SetRXAEMNRNoiseProfile(channel, mag, n)`
   or similar.  Requires maintaining a Lyra-flavored WDSP build —
   ongoing maintenance burden.
2. Skip captured-profile entirely in WDSP mode permanently.
   Rejected by operator — feature is wanted.

**When IQ-domain implementation work begins:**

* Read `scratch/wdsp_port_status.md` first for per-attempt fix
  history (3 failed approaches today) so we don't redo failed
  paths.
* See `_do_demod_wdsp` in `radio.py` for where the apply pass
  USED to live (post-WDSP, audio domain — failed approach).
* See `Radio.set_nr_use_captured_profile` for the existing
  runtime status-bar warning.
* New path: tap IQ in `_do_demod_wdsp` BEFORE `_wdsp_rx.process(iq)`,
  apply spectral subtraction, hand cleaned IQ to WDSP.  Capture
  path needs equivalent IQ tap.
* Block-boundary handling: STFT with 50% overlap-add (Hann window,
  COLA-perfect reconstruction) — same pattern as `nr2.py`'s
  audio-domain implementation.

**Operator-visible behavior in v0.0.9.9 (Phase 4 LIVE):**

* Capture button works (countdown, save dialog) — captures raw
  IQ pre-WDSP at the operator's current rate.
* Captured profiles persist across sessions.
* Toggle "use captured" on → spectral subtraction is applied
  to the IQ stream BEFORE WDSP's RXA chain.  Operator hears
  the noise floor drop ~6-12 dB depending on band conditions.
* INERT status warning REMOVED — apply path is no longer inert.
* Cross-rate profile load → refused with operator-friendly
  "captured at X Hz, current rate is Y Hz, switch back or
  recapture" message (v2 profiles are rate-specific by design).
* Cross-FFT-size profile load → similar refusal message.
* Legacy mode (``LYRA_USE_LEGACY_DSP=1``) — env var no longer
  has any effect (cleanup arc deleted the legacy DSP path).
  v1 audio-domain profiles on disk from pre-v0.0.9.6 → refused
  on load with clear "recapture in v0.0.9.9+" hint.

#### Toggle-pattern UX for §14.6 (operator design lens, 2026-05-09)

When the IQ-domain rebuild lands, the **operator-facing UX should
mirror the NPE picker** — a Settings checkbox or two-way switch on
the DSP+Audio panel:

```
Settings → DSP → Noise reference
  ( ) Off — use WDSP's built-in noise tracker (default)
  ( ) Use captured profile — your QTH-specific spectrum
       Profile: [WX-2026-05-08-7250kHz-quiet ▾]
```

Same as NPE: operator picks "stock algorithm" or "their thing"
depending on which sounds better at the current band conditions.
The captured profile is genuinely operator-specific data (your QTH's
noise floor at that band, that time of day, that antenna), so
flipping the toggle produces a real audible difference — unlike a
hypothetical "trained vs untrained zetaHat" toggle which would be
theater (those datasets are bit-exact identical; see investigation
below).

**Why this is the right framing:**

Operators already understand the NPE pattern (Mode 1-4 mode-of-the-
gain-function picker + AEPF on/off + NPE method picker — three
operator-tunable knobs over WDSP's stock algorithm).  Adding
"reference profile picker" as a fourth knob fits the same mental
model: pick the noise model that matches your situation.

**Implementation hook:**

A `Radio._noise_reference_mode` enum-ish setting:
* `"stock"` — WDSP's noise tracker (current behavior)
* `"captured"` — apply pre-WDSP IQ-domain spectral subtraction
  using the operator-selected captured profile

The `set_nr_use_captured_profile` method already exists and fires
the status-bar warning today.  Rewire it to: "stock" → no IQ-
domain pre-pass; "captured" → enable the IQ-domain pre-pass with
the active profile.  No status-bar warning needed once the
implementation is real.

**Settings persistence:**

Same QSettings keys we already have for the captured profile
selection (`nr/use_captured_profile`, `nr/active_profile_name`).
No schema bump.

#### Forward-compatibility with TX (v0.2) and PureSignal (v0.3)

Operator asked at the start of Phase 2 (2026-05-10) whether the
IQ-domain pre-pass would interfere with TX or PureSignal work
landing in v0.2 / v0.3.  Recorded answer so future sessions don't
re-derive it.

**The pre-pass is wired exclusively into ``Radio._do_demod_wdsp``,
which only ever sees DDC0 (RX1's receive IQ stream).**  TX, PS
feedback, and self-monitoring are independent code paths that
share none of the new code:

| Concern | Status |
|---|---|
| §14.6 affects PureSignal calibration math? | **No** — PS uses DDC2/DDC3 feedback at TX freq (§3.4 / §6.4 — ``ddc[2].freq_source = "TX"``), dispatched to a separate ``ps_calcc.py`` / ``ps_iqc.py`` handler that the captured-profile pre-pass has zero hooks into |
| §14.6 affects TX modulation chain? | **No** — TX is mic → ``ssb_mod.py`` → baseband I/Q → EP2 framing → HL2 PA, totally independent of any RX path |
| §14.6 affects RX1 self-monitoring during TX? | **No** — Wiener gain ``G[k] = max(floor, 1 - profile_mag[k] / frame_mag[k])`` produces ``G ≈ 1`` on strong signals (operator's own carrier bleeding through ≫ profile magnitude), so the pre-pass is transparent to the operator's TX content while still attenuating background noise on RX1 |
| §14.6 affects duplex / ``puresignal_run`` flags? | **No** — C4 bit 2 (duplex) and frame 11/16 C2 bit 6 (``puresignal_run``) are protocol-layer concerns in ``stream.py``; §14.6 doesn't touch the protocol layer at all |
| §14.6 affects RX2 (v0.1)? | **Eventually yes** — RX2 will need its OWN ``CapturedProfileIQ`` instance for its own band's noise spectrum, but that's a clean per-channel duplication (one IQ pre-pass per WDSP channel) handled when v0.1 lands.  Not a cross-cutting concern. |

**On full duplex during PS:** operator was correct that PS needs
the duplex bit set + ``puresignal_run`` flags + nddc=4 — already
documented in §3.2 and §3.7.  None of that protocol surface is
touched by §14.6.  During PS+TX the gateware delivers DDC2/DDC3
feedback at ``rx1_rate`` while DDC0 keeps running RX1 normally;
the captured-profile pre-pass continues running on DDC0 (where
it's transparent to the strong self-monitoring signal per the
table above), and DDC2/DDC3 PS feedback dispatch never enters
``_do_demod_wdsp``.

**Bonus side-property:** when v0.1 RX2 lands, each RX channel can
have its own captured profile (operator listening to 40m on RX1
and 20m on RX2 might want band-specific QTH noise subtraction
on each).  The per-WDSP-channel pre-pass model from Phase 4
naturally supports this — just instantiate a second
``CapturedProfileIQ`` for RX2's IQ stream.

#### Companion investigations (parked alongside §14.6)

These came up while operator was researching the captured-noise
feature.  Cross-linked here so they don't get rediscovered.

**A. Thetis `zetaHat.bin` is identical to WDSP's C-baked default
(verified 2026-05-09).**

The Gemini-style summary the operator was reading suggested
Thetis ships a "trained" gain table file derived from "72 hours
of band noise" — implying a meaningful difference vs the WDSP
default.  Bit-exact diff on Thetis 2.10.3.13:

* `zetaHat.bin` (43,240 bytes) at
  `D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/Project Files/lib/Thetis-resources/zetaHat.bin`
* `CzetaHat[]` baked into WDSP source at
  `D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/Project Files/Source/wdsp/zetaHat.c`

All 3,600 doubles match to 1e-12 (worst real-cell delta = 0.0).
All 3,600 zetaValid integers match exactly.

What the file actually is: a 60×60 lookup table of MMSE-LSA gain
values indexed by (γ, ξ) — the a-posteriori / a-priori SNR pair.
NOT a noise spectrum.  NOT QTH-specific.  Generic algorithm
tuning.  When WDSP's `readZetaHat()` (in `wdsp/emnr.c:207`) can't
find a `zetaHat.bin` in CWD, it falls back to the C-baked array;
when it finds one, it loads from the file.  Either way, ham
operators downstream get the same data because Thetis ships the
same data both ways.

**Implication:** there's no shippable variant of `zetaHat.bin` to
toggle between in stock WDSP.  Modes 3 / 4 in our NR-mode picker
already use this gain table via `gain_method=2` / `gain_method=3`.
Don't waste cycles building a "use Thetis trained table" toggle —
nothing would change.

(The file COULD be regenerated offline by replicating NR0V's
training pipeline, but that's a research project, not a feature.)

**B. Line-synchronous blanking (LSB) — KA7OEI-style.**

Operator-attached doc 2026-05-09 covered software LSB: PLL-locked
time-domain blanker that targets mains-synchronous impulsive noise
(SCR dimmers, switching supplies) at 100/120 Hz.  Linrad's the
canonical reference implementation.

Status: parked, NOT a separate feature.

Reasons:
1. Targets a noise type (mains-locked impulses) that the operator
   doesn't currently report as a top issue.  N8SDR's worst case
   is the nearby AM broadcaster's 5th harmonic on 7.250 MHz — an
   RF interferer, not mains-locked, which LSB does nothing for.
2. WDSP NB at "Heavy" handles impulsive noise reasonably for
   typical operator situations.  No tester has yet reported "WDSP
   NB doesn't kill my dimmer buzz."
3. The IQ-domain captured-profile rebuild (§14.6) is a strict
   superset: capture + replay any periodic spectral pattern,
   mains-locked or otherwise.  An LSB-style PLL variant could
   layer on top of §14.6 ("sync profile to mains") if a real
   need surfaces, but standalone is duplicative.

**If a tester reports unmissable mains-locked impulses that WDSP
NB Heavy + IQ-domain captured-profile both fail to suppress**, then
revisit.  Implementation outline at that point:

* PLL-track the dominant 100/120 Hz pulse train in pre-WDSP IQ
* Compute predicted next-pulse timestamp at sample-clock resolution
* Time-domain gate that zeroes ~50-100 µs around each predicted
  pulse
* Avoids the "static profile vs dynamic AGC" mismatch that killed
  cyclostationary spectral subtraction (§9.5) — different
  domain, different failure modes
* CPU: cheap (a few hundred µs per second of audio)
* UX: "Off / Light / Heavy" picker on DSP+Audio, similar to NB

**C. Modify-WDSP-C-source path for "captured noise as LMS
reference" — REJECTED (per the 2026-05-09 Gemini-doc analysis).**

The Gemini summary the operator forwarded suggested editing
`Thetis/DSP.cs`, `Thetis/WDSP.cs`, AND the WDSP C source itself
to add a "noise-only reference buffer" input to the LMS adaptive
filter.  This path is explicitly out of scope for Lyra:

* Maintaining a Lyra-flavored WDSP fork = ongoing burden every
  time NR0V ships a new WDSP version
* Loses the bundle-the-stock-DLL property of v0.0.9.6 (which we
  picked specifically to avoid compile-chain complexity in
  installs)
* §14.6 IQ-domain pre-WDSP approach achieves the same end-result
  without forking: tap IQ before `_wdsp_rx.process(iq)`, apply
  spectral subtraction in IQ-magnitude domain using the captured
  profile, hand cleaned IQ to WDSP — WDSP sees nothing different
  about its input.  Same NR effect, zero WDSP-source touches.

The Gemini doc is well-written but its recommended path is the
expensive one for our architecture.  Do not pursue.

### 14.7 NR-mode UX overhaul (2026-05-07 evening — IN OPERATOR TESTING)

**Background:** operator-driven UX redesign after extensive A/B
testing showed the legacy NR1/NR2 backend dropdown + dual strength
sliders was confusing in WDSP mode (sliders mostly inert; backend
NR1/NR2 sounded similar even though we set different gain methods).

**New model — Thetis-inspired but Lyra-tuned:**

* **NR enable button** → master on/off (existing button repurposed)
* **NR slider** → 4-position MODE selector (1..4) — replaces the
  legacy "strength" semantics on the same slider widget
* **AEPF checkbox** → anti-musical-noise post-filter (new control)
* **NR2 aggression slider** → HIDDEN entirely in WDSP UI (still
  constructed for legacy code paths)

**Mode mapping** (see `Radio._NR_MODE_TO_GAIN_METHOD`):

| Mode | gain_method (WDSP) | Character |
|---|---|---|
| 1 | 0 (Wiener + SPP) | Smooth, mid-aggressive |
| 2 | 1 (Wiener simple) | Edgier, more raw subtraction |
| 3 | 2 (MMSE-LSA) | WDSP default, smoothest **(default)** |
| 4 | 3 (Trained adaptive) | Most aggressive |

**Files touched:**

* `lyra/radio.py` — `set_nr_mode`, `set_aepf_enabled`,
  `_push_wdsp_nr_state` rewrite, `autoload_nr_mode_settings`,
  signals `nr_mode_changed` + `aepf_enabled_changed`.
* `lyra/dsp/wdsp_engine.py` — already had EMNR/ANR knob methods
  from earlier Option B work this afternoon.
* `lyra/dsp/wdsp_native.py` — already had cffi bindings.
* `lyra/ui/panels.py` — repurposed `nr1_strength_slider` (range
  changed from 0..100 to 1..4, label "Mode:" instead of "NR
  strength:"), added `aepf_checkbox`, hid `nr2_agg_slider`
  layout-wise, slot handlers `_on_nr_mode_slider`,
  `_on_aepf_checkbox`, `_on_nr_mode_signal`,
  `_on_aepf_enabled_signal`.
* `lyra/ui/app.py` — new `autoload_nr_mode_settings` call at
  startup.

**QSettings migration:**

* `nr/profile = nr2` → `noise/nr_mode = 1`
* `nr/profile = nr1` (or anything else) → `noise/nr_mode = 3`
* AEPF defaults ON (`noise/aepf_enabled = True`)
* Old keys preserved for legacy mode

**NPE dropdown — DONE 2026-05-07 evening.**  Initial design proposed
"per-mode npe_method differentiation" (each Mode 1-4 fixed to one
NPE method) but operator pushed for the better answer: surface NPE
as an OPERATOR-TUNABLE control on the DSP+Audio panel.  Now
operator picks Mode + AEPF + NPE independently → Lyra exposes more
WDSP knobs for direct on-air tuning than Thetis / SparkSDR /
PowerSDR (all hide NPE).  Real differentiator.  Operator-confirmed
audible difference between OSMS and MCRA.

**Future polish ideas (still on the table):**

1. **Settings → DSP → NR Advanced panel** — expose `ae_zeta_thresh`,
   `ae_psi`, additional fine-tuning knobs.  Thetis hides these in
   registry; Lyra could expose them in advanced settings.
   v0.0.9.6.x or v0.1 polish.

2. **Mode names instead of numbers** — "Smooth/Raw/Default/
   Aggressive" labels in the UI.  Or numbers + character hint in
   tooltip (currently does this).

**Operator-confirmed status as of 2026-05-07 late-evening:**

* New UX wired + tested + working on real signals
* AEPF checkbox = clear audible difference (operator: "no wonder
  it's hidden and on")
* NPE dropdown = clear audible difference between OSMS and MCRA
* Modes 1-2 sound similar (both Wiener variants); Mode 3 = MMSE-LSA
  smoothest; Mode 4 = "FM-like for SSB" (aggressive trained
  adaptive — useful but distinctive)
* LMS slider works (controls ANR step size mu logarithmically)
* APF works (CW-only, mode-gated)
* Captured-profile both capture AND apply paths work in WDSP
  mode (v0.0.9.9 §14.6 Phase 4 — IQ-domain rebuild landed)

### 14.8 All-mode squelch — WDSP SSQL native (2026-05-07 night)

**TL;DR:** the SQ button in WDSP mode now drives WDSP's native
SSQL ("Single-mode Squelch Level") for SSB/CW/DIG, plus the
existing FM-SQ and AM-SQ modules for those modes.  This is the
WDSP-port-not-Thetis-copy principle in §13.3 applied to squelch:
WDSP ships SSQL; Lyra calls into it via cffi.  Other WDSP
consumers happen to use the same module the same way — they're
sibling consumers of WDSP, not Lyra's reference.  No Python-side
audio-domain gate — multiple attempts at one all failed because
WDSP's AGC compresses voice/noise dynamic range to ~1.5-2×
post-AGC, blinding any audio-RMS gate.

**The journey** (preserved here so future sessions don't repeat
it):

| Attempt | Approach | Failure mode |
|---|---|---|
| 1 | Hand-rolled dBFS RMS gate, slider→absolute threshold | Pre-vs-post-volume position couldn't be calibrated |
| 2 | Move pre-volume + widen dBFS map to -75..-25 | Loose at top — gate stayed open on noise floor |
| 3 | Delegate to legacy `AllModeSquelch` (auto-tracked floor + ratio) | Erratic on real signals; floor seeding broke when SQ enabled mid-signal |
| 4 | Tighten K_OPEN constants for AGC-compressed audio | Closed gate mid-syllable on S9 signals at slider=0.7 |
| 5 | Smarter seed (1-sec min-window) + reverted track-up tau | Better but still hit-and-miss; root cause was AGC compression in audio domain |
| 6 | Spectrum-domain SNR gate (pre-AGC FFT signal vs noise floor) | Worked, but operator pointed out WDSP already ships SSQL for exactly this — call WDSP's instead of building parallel |
| 7 (final) | WDSP SSQL via cffi (`SetRXASSQLRun`/`Threshold`/`TauMute`/`TauUnMute`) | Operator-confirmed working |

**Final config** (in `lyra/radio.py`):

* `_SSQL_SCALE = 0.65` — slider 0..1 multiplied by 0.65 before
  passing to `SetRXASSQLThreshold`.  WDSP's WU2O-tested-good
  default is 0.16; with this scale, slider=0.20 → SSQL=0.13
  (just below WU2O default — comfortable), slider=0.30 → SSQL=0.20
  (slightly tight).  Direct 1:1 mapping put the operator's
  typical slider zone above WU2O default = perceived as tight.
* `_SSQL_TAU_MUTE = 0.7s` — vs WDSP `create_ssql` default 0.1s.
  WDSP's source comment notes "reasonable wide range is 0.1 to
  2.0".  WDSP's window detector (`wdaverage`) has a hardcoded
  0.5s adaptation tau; on quasi-stationary signals (continuous
  SSB conversation, digital modes) the average converges to the
  signal level within 1-2 sec → SSQL flags "no signal" → trigger
  voltage rises toward mute.  With the WDSP default
  tau_mute=0.1s, that false flag becomes a clamp in 134 ms.  At
  0.7s, trigger rise is ~940 ms — long enough that brief window-
  detector convergences don't clamp the gate while genuine end-
  of-transmission still mutes within ~1 sec of speech ending.
  Operator-tuned through 1.0s → 0.7s.
* `_SSQL_TAU_UNMUTE = 0.1s` — matches WDSP default.  Snappy
  speech-onset response.

**Routing** (`_push_wdsp_squelch_state` in radio.py):

* Mode FM → `SetRXAFMSQRun` (existing FM SQ)
* Mode AM/SAM/DSB → `SetRXAAMSQRun` + threshold (existing, dB-scaled)
* Mode SSB/CW/DIG/SPEC → `SetRXASSQLRun` + threshold (NEW)
* Disables the inactive modules to prevent crosstalk
* Called from `set_squelch_enabled` (operator toggle),
  `set_mode` (handoff between FM ↔ AM ↔ SSQL on mode change),
  and `_open_wdsp_rx` (initial state on stream start)

**Cffi bindings** (`lyra/dsp/wdsp_native.py`): `SetRXASSQLRun`,
`SetRXASSQLThreshold`, `SetRXASSQLTauMute`, `SetRXASSQLTauUnMute`.
**Engine wrappers** (`lyra/dsp/wdsp_engine.py`): `RxChannel.set_ssql_*`
methods on the `RxChannel` class.

**Files no longer in WDSP audio path** (legacy fallback —
**DEPRECATED**, see §14.9 below):

* `lyra/dsp/squelch.py` (`AllModeSquelch`) — only runs when
  `LYRA_USE_LEGACY_DSP=1`.  Constants reverted to original
  `K_OPEN_BASE=1.5 / K_OPEN_RANGE=6.0` / 150 ms seed.

**Hot points to remember if it comes back up:**

* Don't reach for a Python-side audio-domain gate.  The whole
  arc proved this can't work — AGC compresses signal/noise to
  the point that no audio-RMS threshold reliably distinguishes.
* If operator perception drifts again, the knobs are
  `_SSQL_SCALE` (overall slider feel), `_SSQL_TAU_MUTE` (clamp
  delay on convergence transients), `_SSQL_TAU_UNMUTE` (unmute
  responsiveness).  WDSP's `wdtau` (window-detector adaptation
  speed) is hardcoded inside the DLL at 0.5 sec — would need a
  WDSP rebuild to change.

### 14.9 Legacy pure-Python DSP path — DELETED (cleanup arc complete, 2026-05-08)

**Status: complete.**  v0.0.9.6 retired the pure-Python DSP path
in favor of WDSP cffi as the single audio engine.  Cleanup
landed across the `feature/v0.0.9.6-audio-foundation` branch in
~16 commits between 2026-05-07 night and 2026-05-08 evening.

#### What got deleted

| Module / file | Lines | Replaced by |
|---------------|-------|-------------|
| `lyra/dsp/leveler.py` (`AudioLeveler`) | 355 | feature retired (WDSP AGC handles dynamic range) |
| `lyra/dsp/agc_wdsp.py` (`WdspAgc`) | 746 | WDSP cffi `SetRXAAGCMode` directly |
| `lyra/dsp/apf.py` (`AudioPeakFilter`) | 251 | WDSP SPEAK biquad via `_push_wdsp_apf_state` |
| `lyra/dsp/demod.py` (5 demod classes + `NotchFilter`) | 528 | WDSP RXA chain (decim + demod + notches inside cffi engine) |
| `lyra/dsp/nb.py` (`ImpulseBlanker`) | 477 | `_NBState` dataclass + WDSP NOB |
| `lyra/dsp/lms.py` (`LineEnhancerLMS`) | 459 | `_LMSState` dataclass + WDSP ANR |
| `lyra/dsp/anf.py` (`AutoNotchFilter`) | 395 | `_ANFState` dataclass + WDSP ANF |
| `lyra/dsp/squelch.py` (`AllModeSquelch`) | 419 | `_SquelchState` dataclass + WDSP SSQL/FMSQ/AMSQ |
| `lyra/dsp/nr2.py` (`EphraimMalahNR`) | 1496 | `_NR2State` dataclass + WDSP EMNR |
| `lyra/dsp/channel.py::PythonRxChannel.process()` | ~600 | WDSP `RxChannel.process()` via `_do_demod_wdsp` |
| `Radio._apply_agc_and_volume` | ~168 | volume / mute applied in `_do_demod_wdsp` directly; AGC/AF Gain/APF live in WDSP |
| `LYRA_USE_LEGACY_DSP=1` env-var fallback dispatch | ~57 | gone — WDSP is the only path |
| 2 diag scripts (`diag_agc_wdsp_*.py`) | ~300 | obsolete (Python AGC port deleted) |
| Settings dialog: NR2 group + NR2 Gain Function picker + LMS-strength duplicate | ~290 | DSP+Audio panel (NR Mode 1-4 + AEPF + NPE + LMS strength) covers it |
| `panels.py` orphan NR2 strength slider + gain-method right-click menu | ~120 | same — Mode 1-4 picker is the live surface |
| `block_size` kwarg on `PythonRxChannel.__init__` | trivial | unused after `process()` deletion |

**Cumulative**: ~6,800 lines of legacy code removed.

#### Bugs fixed during cleanup (operator-reported, all in r3 baseline)

1. **AF Gain inert in live audio** — `_apply_agc_and_volume` had been the only consumer of `af_gain_linear` for live signal, and that method had been orphan since Phase 4.  Fixed in Phase 6.A1 by wiring `set_af_gain_db` to `_wdsp_rx.set_panel_gain(af_gain_linear)`.
2. **AGC Settings sliders didn't follow profile changes** — `set_agc_profile` updated the profile but never read the preset table to update advisory `_agc_release` / `_agc_hang_blocks`.  Fixed in Phase 6.A2 plus widening the Release slider range (was clamping Fast preset).
3. **AGC threshold push missing** — `_open_wdsp_rx` configured AGC mode but not threshold; engine ran with create-time max_gain default which prevented the gain meter from moving.  Fixed in Phase 6.A3 + fix-up by wiring `set_agc_slope(0)` + `set_agc_threshold(thresh_db, 4096, in_rate)` at init.
4. **FM SQ slider had no effect** — `_push_wdsp_squelch_state` called `SetRXAFMSQRun` but never `SetRXAFMSQThreshold`; FM mode ran at engine create-time threshold (0.750) regardless of slider.  Fixed in Phase 6.A4 with logarithmic mapping `10^(-2·v)`.
5. **ANF μ slider was advisory-only** — operator's μ value was persisted on the dataclass but never reached WDSP.  Fixed in Phase 6.A4 by adding `SetRXAANFVals` binding + wrapper + push from `set_anf_mu` and `_open_wdsp_rx` init.
6. **AM SQ tail too long** — engine default 1.5 s felt unnaturally long.  Fixed in Phase 6.A4 by pushing 0.5 s at `_open_wdsp_rx` init via new `SetRXAAMSQMaxTail` binding.
7. **AM/DSB squelch stuck on master-off** — `_push_wdsp_squelch_state` mode-routing logic skipped disabling the active-mode SQ module when SQ went off (only handled mode-mismatch disables + SSQL).  Fixed in Phase 6.A4 fix-up by pulling the master-off check above the mode-targeted disables.

#### Architecture state now

```
HL2 (HPSDR P1) → UDP IQ → HL2Stream → DspWorker.process_block
    → Radio._do_demod_wdsp (one method, ~120 lines)
        → _wdsp_rx.process(iq)              # decim + notches + demod + NR + ANF + AGC + APF inside cffi
        → volume / mute / capture-feed     # Python-side post-processing
        → BinauralFilter (BIN, optional)
        → audio_sink.write
```

Lyra retains operator-state mirrors on `PythonRxChannel`:
* `_nr` — real `SpectralSubtractionNR` instance (NR1 capture machinery — only nr.py interface still alive; powers the 📷 Cap button).
* `_apf`, `_nb`, `_lms`, `_anf`, `_squelch`, `_nr2` — `_*State` dataclasses (operator-tunable knobs persisted across sessions; pushed to WDSP via `_push_wdsp_*_state` helpers).

The `DspChannel` ABC is kept for forward compatibility (a future DSP backend could subclass it), but its `process()` abstractmethod is gone — channels are state containers now, not DSP drivers.

#### Tags + bundles for archaeology

| Tag | What it covers |
|-----|---------------|
| `v0.0.9.6-rx1-working-r3` | Pre-cleanup baseline (operator-verified WDSP working) |
| `v0.0.9.6-rx1-working-r4` | + AM right-channel-silent fix (§14.10) |
| `v0.0.9.6-rx1-working-r5` | + Phase 4 (Audio Leveler delete) |
| `v0.0.9.6-rx1-working-r6` | + Phase 5 (channel.py slim to state container) |
| `v0.0.9.6-rx1-working-r7` | + Phase 6.A + 6.A1 (orphan delete + AF Gain fix) |
| `v0.0.9.6-rx1-working-r8` | + Phase 6.B/C + Sweep 1 + Phase 7 + AGC plumbing |
| `v0.0.9.6-rx1-working-r9` | Cleanup arc COMPLETE (Phase 8 + Phase 9 polish + 7 operator-reported bug fixes) |

Each tag has a matching portable bundle in `_backups/lyra-2026-05-08-rx1-working-rN.bundle`.  Restore via `git clone _backups/<bundle> restored-lyra`.

If anyone needs to recover a deleted file by name (e.g. the spectral-subtraction port for a future captured-profile IQ-domain rebuild), `git show <tag>:lyra/dsp/<file>.py` walks the tree at any tag's snapshot.

#### Follow-ups still open (NOT part of cleanup arc)

* ~~**§14.6 Captured-profile IQ-domain rebuild**~~ **CLOSED 2026-05-10 (v0.0.9.9):** IQ-domain rebuild landed across Phases 1-4.  Schema v2 (rate-specific full complex-FFT magnitudes), `CapturedProfileIQ` STFT engine in `lyra/dsp/captured_profile_iq.py`, capture + apply both wired in `_do_demod_wdsp` pre-WDSP.  v1 audio-domain profiles refused on load with recapture hint.  Still pending: Phase 5 (Settings FFT-size dropdown + DSP panel badge polish) and Phase 6 (operator A/B test matrix).
* **§14.10 _open_wdsp_rx audit (partially closed)** — Phase 6.A3 + 6.A4 wired the AGC + FM SQ + ANF + AM SQ gaps the audit found.  Lower-priority gaps (FM Deviation, FM Limiter, FM AF Filter, CTCSS, AM DSBMode, AM Fade, NR3-RNNoise, NR4-SpectralBleach, EMNR Position, ANR Position, Pan, etc.) deferred until operator surfaces specific need.
* ~~**HL2 audio smoothing regression check** — Phase 9.5 Item 2.  A "less harsh" smoothing change landed during the v0.0.9.6 audio rebuild on 2026-05-07 may have been dropped during a subsequent revert chain.  Worth a `git log -p lyra/dsp/audio_sink.py` review.~~  **CLOSED 2026-05-09: NO regression.**  The smoothing change in question was Option Z (commit `022d1fd`, half-cosine slewed-silence-fill on EP2 underrun, 2026-05-06 12:47).  It was deliberately reverted (`f29f53d`, 12:56) when the real root cause was found 19 minutes later: HL2 command 0x17 (`config_txbuffer`) was never being sent, so the FPGA's TX-side audio buffer ran at the gateware default 10 ms and underran with Python-side jitter.  The actual fix landed in `c7916bc` (13:15) and lives at `lyra/protocol/stream.py:356` as the `0x2e` register entry (`(0, 0, 12 & 0x1F, 40 & 0x7F)` = 12 ms PTT hang, **40 ms TX latency**), pushed at startup via the standard C&C cycle.  Plus TPDF dither (stream.py:207-260) and S-meter peak-hold smoothing (radio.py:1358 `_SMETER_PEAK_DECAY = 0.85`) are also still in place.  The revert was correct — Option Z would have masked symptoms while c7916bc fixes the cause.  No code action; CLAUDE.md note kept here as the audit trail in case anyone re-reads §14.9 and wonders why the strikethrough.
* **AGC profile A/B at the operator level** — meter movement is verified (Phase 6.A3 fix), but per-time-constant audible differences (Fast vs Slow vs Long on real speech / CW) need operator confirmation when band conditions improve.

### 14.10 AM/FM/DSB right-channel-silent bug — FIXED (2026-05-07 night)

**Operator-reported symptom:** in AM, DSB, and FM modes, only the
LEFT audio channel produced sound; the BAL slider had no effect on
the right (full-right = silence).  SSB (USB/LSB/CWU/CWL) worked
normally.  Affected both HL2 audio jack and PC Soundcard paths.
Bug was present in `ce70e97` ("RX1 audio foundation milestone")
but had escaped operator verification because the prior test pass
focused on SSB modes.

**Root cause:** WDSP's EMNR (`emnr.c:1247-1248`) explicitly zeroes
the Q channel on output:
```c
a->out[2 * i + 0] = a->outaccum[a->oaoutidx];   // I = noise-reduced audio
a->out[2 * i + 1] = 0.0;                         // Q forced to zero
```

For SSB modes, the post-EMNR `xbandpass(bp1)` stage has an
**asymmetric** passband (USB = positive freq only, LSB = negative
freq only).  A complex bandpass with one-sided passband acts as a
Hilbert restorer — the output Q is reconstructed analytically from
the real input I, and stereo content survives.

For AM/FM/DSB, the post-EMNR BP1 has a **symmetric** passband
(`-W..+W` around DC).  Real input through symmetric complex
bandpass → real output (output Q stays zero).  Q remains zero
through the patch panel and all the way out the audio sink.

The patch panel's behaviour is determined by its `copy` field:
* `copy=0` (default from `create_panel`): no copy.  L = gain1 * I,
  R = gain2Q * Q.  Q=0 → R=silence.
* `copy=1`: copy I to Q at panel output.  L = gain1 * I,
  R = gain2Q * I.  Mono on both channels regardless of upstream Q.

WDSP's `create_panel` defaults to `copy=0`.  Thetis explicitly
calls `SetRXAPanelBinaural(0)` at channel init, which sets
`panel.copy = 1 - 0 = 1` — overriding the create-time default.
Lyra never made that call, so we inherited `copy=0` and AM/FM/DSB
silenced the right channel whenever EMNR was active (which is
"basically always" since NR Mode 1-4 are EMNR variants).

**The fix:**

* `lyra/dsp/wdsp_native.py`: cdef `SetRXAPanelBinaural`
* `lyra/dsp/wdsp_engine.py`: `RxChannel.set_panel_binaural(bool)`
  wrapper.  `False` = mono on both channels (= panel.copy=1,
  matches Thetis's default listening setup).  `True` = no copy
  (= panel.copy=0, raw I/Q routed to L/R, available as an escape
  hatch for raw-IQ binaural listening if anyone ever asks).
* `lyra/radio.py` `_open_wdsp_rx`: call `set_panel_binaural(False)`
  right after `set_panel_gain(1.0)`.  Persists for the life of the
  WDSP channel; mode changes don't disturb it.

**Verified across all modes** with EMNR enabled:

| Mode | L_rms | R_rms | Status |
|---|---|---|---|
| LSB | 0.5325 | 0.5325 | ✓ |
| USB | 0.5507 | 0.5507 | ✓ |
| AM | 0.7701 | 0.7701 | ✓ |
| FM | 0.5454 | 0.5454 | ✓ |
| DSB | 0.7071 | 0.7071 | ✓ (operator confirmed BAL pans cleanly) |
| CWU | 0.5636 | 0.5636 | ✓ |

**Compatibility note for v0.1 RX2 stereo split** (per operator
question 2026-05-07 night): this fix is the *correct* foundation
for split-mode stereo, not a problem for it.  WDSP's per-channel
`SetRXAPanelBinaural` controls intra-RX I/Q-to-L/R routing
(unrelated to multi-RX stereo).  Lyra's RX2 stereo split lives
in `AudioMixer` per §6.1: each RX produces mono-on-stereo, then
the mixer pan-curves RX1 hard-left and RX2 hard-right.  With our
fix, each individual RX channel reliably produces mono output
that the mixer can spatially pan; without our fix, panning RX1
hard-left would lose audio (only the I component would survive,
and EMNR could zero it on the way through).

**Audit reminder:** this bug surfaced because Lyra's
`_open_wdsp_rx` skipped a setter Thetis calls.  There are likely
more.  Future audit: diff the `SetRXA*` calls in our
`_open_wdsp_rx` against Thetis's channel-init sequence in
`Console/radio.cs`, looking for siblings like:

* `SetRXAPanelGain1`/`Gain2` defaults — we set Gain1=1.0, leave
  Gain2I/Gain2Q at create_panel defaults of 1.0 each.  Probably OK.
* FM-deemphasis settings — Thetis sets these per-mode.
* SBNR / RNNR (NR3 / NR4 in Thetis) — we don't bind them at all.
* Notch DB filter coefficients — currently push freqs only;
  Thetis pushes BW + run + tune freq.
* AGC fixed-gain / hang threshold per AGC mode.
* CESSB / CFC TX-side equivalents (when v0.2 TX work begins).

Track in `docs/architecture/measurements_and_cleanup.md` as a
phase before TX work starts.

---

## 15. Documentation backlog from v0.0.9.6.1 audit (2026-05-09)

Two-agent audit during the v0.0.9.6.1 release prep flagged these
items.  High-priority operator-facing fixes landed in the patch
itself (NR right-click menu name fix, AGC profile cleanup of stale
"Long" entry, ANF profile name correction in troubleshooting.md,
captured-profile WDSP-mode INERT caveat, AGC Auto profile docs
correction).  The items below are non-blocking and parked for a
future session.

### 15.1 — Internal architecture doc cleanup (CLOSED 2026-05-10)

All three items closed in the v0.0.9.8.x doc cleanup pass:

* **`CLAUDE.md` "Current version" line** — replaced with a
  pointer to ``lyra/__init__.py`` so the line doesn't go stale
  again.
* **§14.2 "Wired" / "Inert" lists** — rewritten.  Wired list
  reflects the v0.0.9.6 NR-mode UX overhaul (Mode 1-4 + AEPF +
  NPE), v0.0.9.6 manual-notches / NB UI / BIN / APF wiring, and
  v0.0.9.8's central DDS offset for the carrier-freq VFO
  convention.  Inert list pruned to just the genuinely-deferred
  items: captured-profile apply (IQ-domain rebuild per §14.6),
  NR3/NR4 (DLLs bundled but no UI), TX/PS chains (Phase v0.2/v0.3).
  Audio Leveler removed entirely (deleted, not parked).
* **"Last updated" trailer** — refreshed to 2026-05-10 with the
  v0.0.9.7 → v0.0.9.7.1 → v0.0.9.7.2 → v0.0.9.8 sprint summary
  + §15 backlog pointers.

### 15.2 — RX2 plan leveler references (`docs/architecture/v0.1_rx2_consensus_plan.md`)

Multiple lines (422, 426, 753-757, 789, 804, 1091) still reference
`leveler` as part of the RX/TX audio chain or as a tap point for
the Lit-Arc `MODE_COMP` indicator.  Audio Leveler was DELETED in
the v0.0.9.6 cleanup arc (`lyra/dsp/leveler.py`, 355 lines, see
§14.9).  Action when v0.1 work begins:

1. Update RX/TX chain diagrams to drop the `→ leveler` step (or
   replace with explicit `Vol → APF → sink` to match current
   reality).
2. Re-think `MODE_COMP` signal source — `radio._leveler._env_db`
   no longer exists.  Options: (a) read AGC gain magnitude from
   `radio.agc_action_db` as a proxy for compression; (b) tap APF
   peak gain when active; (c) port WDSP `compress.c` for v0.2 TX
   first then re-use for RX MODE_COMP.
3. TX chain table row (line 1091) `| leveler | lyra/dsp/leveler.py
   (existing RX leveler reused) | ...` — needs either re-port
   from WDSP `compress.c` or alternate strategy.

### 15.3 — Settings dialog connection-tracking refactor closure

`v0.1_rx2_consensus_plan.md` §7.x parks the dead-widget refactor.
The v0.0.9.6.1 sweep landed the partial fix (`_safe_mirror`,
`_swallow_dead_widget`, three-paragraph intro split).  Section
should note that the noise-suppression layer is in but the
DEEPER fix (actual disconnect-on-close) is still parked, with a
pointer to the present helpers as the "current state of the
art."

### 15.4 — Help-doc minor polish (CLOSED in v0.0.9.6.1)

All three items closed during the v0.0.9.6.1 doc audit:

* **Live-preview during zoom slider drag** — added a paragraph to
  `docs/help/spectrum.md` "Update rates and zoom" section noting
  that Spec / WF sliders commit ~10 times per second while held,
  not just on release.
* **`docs/help/bin.md` audio-chain diagram** — redrawn to show the
  WDSP-mode reality (engine handles decim → notches → NR → ANF →
  AGC → APF → bandpass → demod internally; Python layer does
  mute → Volume → BIN → sink).  No more `tanh` stage (that was
  legacy pure-Python).
* **Author attribution** — reconciled to match `CONTRIBUTORS.md`
  authoritative list:
    * `introduction.md` — N8SDR is project lead and sole developer
      through v0.0.9.x; N9BC joined as co-contributor during
      v0.0.9.1 testing; **joint development begins at v0.1**.
    * `support.md` — "primarily built by N8SDR, with N9BC joining
      as co-contributor" (was "built by one person").
    * `license.md` — already had both names in copyright; left
      as-is.

### 15.5 — `_AGC_PROFILES` Long re-add (CLOSED 2026-05-10)

Done.  `panels.py:3835 _AGC_PROFILES` now includes `"long"`
between `"slow"` and `"auto"`; matching entries added to
`_AGC_PROFILE_LABELS`, `_AGC_PROFILE_COLORS`, and
`_AGC_PROFILE_TEXT` (label "Long", amber, text "LONG").  Long
mentions restored in `agc.md` (table row + label color note +
right-click menu list + AM-fade tip), `index.md` (Quick Start
+ Topic index), and `troubleshooting.md` (AGC pumping recipe).
The full radio-side wiring already existed (release time
0.040 s, hang_blocks 46, WDSP mode mapping `"long" → "LONG"`)
since the v0.0.9.6 cleanup arc — only the UI exposure was
missing.

---

*Last updated: 2026-05-10 — v0.0.9.8 "Display Polish" CW VFO
convention switch shipped.  VFO LED now reads the carrier of the
tuned signal in every mode (matching the standard convention used
across major HF SDR applications); central DDS offset in
``Radio._compute_dds_freq_hz`` replaces the per-call-site offsets
from v0.0.9.7.1 / v0.0.9.7.2.  v0.0.9.7.2 was committed and
tagged but skipped on GitHub release — superseded by the
convention switch.  Earlier 2026-05-10: v0.0.9.7.2 spot-pitch
fix (now reverted), Thetis spot-handling research that informed
the convention switch decision.  2026-05-09: v0.0.9.7 "Display
Polish" main release (Peak Hold combo + Decay + Clear, Exact /
100 Hz quantization, spec/wf zoom slider live-preview, spectrum
trace fill master toggle + custom color, waterfall collapse
toggle, per-band waterfall persistence, Settings dialog
hardening) and v0.0.9.7.1 NCDXF tuning fix.  2026-05-08:
v0.0.9.6 "Audio Foundation" final release + the cleanup arc
finishing up (Phase 4-9: Audio Leveler delete + agc_wdsp / apf /
demod / nb / lms / anf / squelch / nr2 deletion + state-container
dataclasses replacing Python DSP modules + AGC plumbing fixes).
2026-05-07 late night: Phase A of legacy-DSP cleanup landed +
§14.10 AM/FM/DSB right-channel-silent fix + §14.8 WDSP SSQL +
§14.7 NR-mode UX overhaul + §14.6 IQ-domain captured-profile
architectural plan.  2026-05-07: RX1 polish push (APF + BIN-
PC-Sound + dither + S-meter peak-hold + capture-feed + LMS
slider wiring + EMNR gainMethod + AEPF cffi bindings).
2026-05-06: §14 added when RX1 went live on the native engine.
2026-05-06: §13 audio architecture decision.  2026-05-02:
senior-engineering pass that produced `implementation_playbook.md`.

§15 backlog (post-v0.0.9.8):
* §15.2 — RX2 plan leveler refs cleanup (file deleted; plan
  references stale)
* §15.3 — Settings dialog deeper disconnect-on-close refactor
  (noise-suppression layer landed v0.0.9.6.1 / v0.0.9.7;
  proper fix parked for v0.1)
* §15.5 — ``_AGC_PROFILES`` Long re-add (one-line code +
  doc restore — see entry for the change set)
* v0.1 RX2 Phase 0 (multi-channel refactor, no behavior change)

Update this file when key decisions change.*
