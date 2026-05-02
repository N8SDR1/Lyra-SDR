# Thetis architecture study: RX2, SPLIT, and PureSignal

Reference notes from a deep-read of the openHPSDR Thetis 2.10.3.13 source
tree at `D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/`.  These inform Lyra's
v0.0.9 RX2 work plus forward-looking notes for SPLIT TX and PureSignal.

**No code copied** — this is a description of patterns, file paths, and
protocol behavior in our own words.

---

## 1. RX2 architecture

### 1.1 Protocol layer

Thetis treats RX2 (and any additional receivers) uniformly: each receiver
corresponds to a DDC slot in the FPGA, and the host enables some subset
of slots.  Plumbing concentrated in
`Project Files/Source/ChannelMaster/networkproto1.c` and the radio-side
controller `Project Files/Source/Console/console.cs::UpdateDDCs(...)`.

Key Protocol-1 facts visible in `networkproto1.c::WriteMainLoop` (and the
HL2 variant `WriteMainLoop_HL2`):

- The control structure is a series of 17 (or 18 on Anvelina Pro 3) C&C
  frame slots, indexed by `out_control_idx`.  Frame 0 is the "general
  settings" frame and carries, in C4:
  - bits [1:0] — antenna select
  - bit 2 — "duplex bit" (always set — see below)
  - bits [6:3] — `(nddc - 1)`, i.e. the number of DDCs to run
  - bit 7 — diversity (locks VFOs when set)
- Frame 1 is TX VFO frequency (32 bits big-endian over C1..C4, in Hz, as
  a 32-bit phase word for P2).
- Frame 2 is "RX1 VFO (DDC0)".
- Frame 3 is "RX2 VFO (DDC1)" — **this is the bit Lyra needs**.
- Frames 5–9 are RX3..RX7 frequencies for 5-DDC radios (Orion class).

Multiple things to note for HL2:

- The duplex bit is unconditionally set in the Hermes/HL2 frame-0 path.
  Matches Lyra's existing memory note.  It is **not** related to
  PureSignal full-duplex feedback; it is a long-standing quirk where,
  without it, the radio refuses to honor RX-only frequency updates.
  Lyra's `stream.py` already sets it; that stays.
- For HL2 (and the Hermes family), Thetis configures `nddc = 4` even
  when only RX1 is active (`UpdateDDCs` HERMES/HERMESLITE branch).  The
  `nddc` field controls how many DDCs of I/Q data are interleaved into
  the EP6 receive frames; only the DDCs that are *enabled* via
  `EnableRxs(DDCEnable)` actually produce non-zero data.
- The "RX2 frequency" (DDC1) under Hermes/HL2 is sent in frame 3 by
  reading `prn->rx[1].frequency`.  When PureSignal is running on a
  2-DDC radio (HermesII path; `nddc == 2`), DDC0 is reused for the TX
  frequency feedback rather than RX1.

### 1.2 EP6 receive stream multiplexing

The receive thread `MetisReadThreadMainLoop` (and `_HL2`) parses
1024-byte UDP payloads as two 512-byte HPSDR frames.  Inside each
512-byte frame:

- bytes 0..2: sync `0x7F 0x7F 0x7F`
- bytes 3..7: control bytes C0..C4 (PTT, ADC overload, fwd/rev power,
  AIN voltages, optional I2C-readback for HL2)
- bytes 8..511: interleaved I/Q + mic samples

Samples-per-DDC formula: `spr = 504 / (6 * nddc + 2)`.  Each "slot" in
the 504 sample-bytes contains `nddc * 6` bytes of I/Q (3 bytes I + 3
bytes Q per DDC, signed 24-bit big-endian) followed by 2 bytes of mic.

| `nddc` | spr | per-slot layout |
|---|---|---|
| 1 | 63 | `[I0(3) Q0(3) mic(2)]` |
| 2 | 36 | `[I0(3) Q0(3) I1(3) Q1(3) mic(2)]` |
| 4 | 19 | `[I0..Q3 mic]` |

Crucially: **DDC sample rate is per-DDC** (`SetDDCRate(i, Rate[i])`).
RX1 and RX2 can run at different rates.  The samples-per-frame stays
constant; the wall-clock pacing differs per DDC.

After parsing, Thetis interleaves DDC0 and DDC1 into a "stitched"
buffer (`twist(spr, 0, 1, 0)`) for delivery to WDSP via `xrouter`.  The
mic sample (TX from radio = TLV320 codec output) is decimated by
`mic_decimation_factor` and fed into a separate inbound buffer
regardless of `nddc`.

### 1.3 DSP layer (WDSP)

WDSP channel IDs are `(transceiver, subchannel)` flattened by
`WDSP.id(t, sr)`:

- `id(0, 0)` = RX1 main
- `id(0, 1)` = RX1 sub-receiver
- `id(1, 0)` = TX
- `id(2, 0)` = RX2 main
- `id(2, 1)` = RX2 sub-receiver

`Radio` constructs a 2x2 array of `RadioDSPRX` instances — two
transceivers, each with main + sub.  RX1 and RX2 are **fully separate
WDSP channel chains**.  Filters, AGC, NR, NB, ANF, squelch, EQ are
duplicated per chain.  Sample rates set independently (`rx1_dsp_rate`
vs `rx2_dsp_rate`).

`cmaster.c::create_rcvr()` loops `cmRCVR` times, building a noise-
blanker chain, opening a DSP channel via `OpenChannel`, and creating a
panadapter analyzer.  At the WDSP level, RX2 is just another receiver
in the cmRCVR loop.

**Threading**: `NUM_RX_THREADS = 2` is named in `radio.cs`, but in
practice WDSP channels run synchronously on whichever thread feeds
samples.  The EP6 reader is one thread (`MetisReadThreadMain`,
MMCSS "Pro Audio", priority +2).  Actual DSP happens inside
`Inbound`/`xrouter` callbacks driven by the receive thread, with
output buffers handed to the audio mixer (`aamix`).  No per-RX worker
thread.

### 1.4 Audio routing

In `console.cs::UpdateAAudioMixerStates()`, RX2 audio routes via the
`aamix` (anti-VOX/audio mixer) using bit-flag masks:

```
RX1   = 1 << id(0,0)
RX1S  = 1 << id(0,1)
RX2   = 1 << id(2,0)
MON   = 1 << id(1,0)   (TX monitor / sidetone)
```

Mask `RX1+RX1S+RX2+MON` is the "data-flow inputs" set; a second mask is
the "mix into speaker output" set.  When RX2 is disabled it drops from
the mask.  Default mix is "everything to one stereo bus" — Thetis does
**not** split RX1-left / RX2-right by default.  Stereo split is per-RX
via `SetRXAPanelPan` in WDSP.  VAC has a separate stereo flag
(`vac_stereo`/`vac2_stereo`) that lets RX2 push to a second virtual
cable.

UX summary:
- Default: both RXs into the same output device; pan slider per-RX.
- Optional: VAC2 enabled, routed to a different host audio device,
  sending RX2 to its own destination.

### 1.5 UI integration

RX2 is **not a separate window** — duplicated control band on the main
`console` form.  Doubled controls everywhere: `comboAGC`/`comboRX2AGC`,
`chkRX2NB`, `chkRX2Squelch`, `chkRX2DisplayPeak`, `chkRX2ANF`, `chkRX2`,
`txtVFOBFreq`, etc., named symmetrically.  RX2 visibility toggled by
`chkRX2.Checked` → `RX2Enabled` setter.  The setter calls
`UpdateDDCs(rx2_enabled)`, `UpdateAAudioMixerStates()`,
`WDSP.SetChannelState(id(2,0), 1, 0)`, kicks off RX2 meter/SQL threads,
and updates display modes (Panafall/Panascope removed from RX1's mode
list because RX2 takes the bottom half).

Display side: one analyzer per receiver (`XCreateAnalyzer(i, ...)` per
DDC in `cmaster.c::create_rcvr`), with the panadapter/waterfall
component switching between single-RX modes (Spectrum, Pandapter,
Panafall, Panascope) and dual-RX layouts that allocate the bottom half
to RX2.

---

## 2. SPLIT operation

Thetis maintains a 2-VFO mental model:

- **VFO A** = RX1's tuned frequency, plus (when not in SPLIT) the TX
  frequency.
- **VFO B** = RX2's tuned frequency *if* RX2 is enabled.  If RX2 is
  disabled, VFO B is a "shadow" frequency used for SPLIT TX and for the
  on-screen sub-readout (`VFOASubFreq`) inside multi-RX-on-single-DDC.

Three checkbox states drive operation:

- `chkRX2.Checked` — enables RX2 DDC + DSP chain.
- `chkEnableMultiRX.Checked` — runs a sub-receiver inside RX1's DDC at
  VFO B's frequency (still one DDC, sub-channel offset).
- `chkVFOSplit.Checked` — TX uses VFO B; RX continues per RX1/RX2.

The SPLIT toggle handler `chkVFOSplit_CheckedChanged` does **not**
enable RX2 or change DDC count — just retags VFO B as the TX source and
updates `VFOASub`.  FM-related TX-shift controls disabled while SPLIT
is on.

### 2.1 The pile-up workflow

Two paths for "I want to hear both the DX op and the up-pile":

1. **One DDC, sub-receiver mode** (`chkEnableMultiRX`): one DDC at
   RX1's center, two WDSP RX channels at `VFOAFreq` (DX op) and
   `VFOASubFreq` (up-pile).  Both must be within the same DDC's
   bandwidth.  Cheap on bandwidth, limited by DDC sample rate.
2. **Two DDCs, RX2 enabled** (`chkRX2`): RX1 listens to VFO A (DX op),
   RX2 listens to VFO B (where the pile-up is calling).  With
   `chkVFOSplit` *also* on, TX kicks over to VFO B's frequency.

Semantics in the pile-up case (RX2 enabled + SPLIT on):

- RX1 = VFO A = DX op's transmit frequency (you hear his "73", "QRZ",
  his pace).
- RX2 = VFO B = listening-up frequency (the chorus of callers).
- TX uses VFO B (you call up).

This is encoded in `bool moxRX1 = _mox && (VFOATX || (VFOBTX && !RX2Enabled));`
and `bool moxRX2 = _mox && (VFOBTX && RX2Enabled);` (around line 22191).
When RX2 is enabled and `VFOBTX` (the SPLIT TX-on-B route) is set, MOX
state pushes DSP-MOX onto RX2's chain rather than RX1's.  `VFOBTX`
effectively means "TX uses VFO B's frequency"; whether it ties to RX2's
chain or RX1's chain depends on `RX2Enabled`.

### 2.2 Quick-swap and copy

Three buttons drive VFO ergonomics:

- `btnVFOAtoB_Click` — copies VFO A frequency, mode, filter into VFO B
  (and into RX2's DSP chain when RX2 is enabled).  Useful before SPLIT.
- `btnVFOBtoA_Click` — inverse.
- `btnVFOSwap_Click` — full A↔B swap, including modes, filters, AGC.

Andromeda hardware buttons mirror these via `OtherButtonId.A_TO_B`,
`B_TO_A`, `SWAP_AB`.  Separate `vfob_lock` mechanism freezes VFO B
against accidental tuning.

### 2.3 PTT behavior and audio during TX

While transmitting in SPLIT, Thetis mutes/fades RX-side audio depending
on options.  Around line 30276:

- If `chkFullDuplex` is **off** (normal), `RX1_shutdown` is computed as
  `chkVFOATX.Checked || (chkVFOBTX.Checked && !RX2Enabled) ||
  mute_rx1_on_vfob_tx || (chkVFOBTX.Checked && ANAN-10E && PSEnabled)`
  — RX1 is hard-shut during TX in many configurations.
- If `chkFullDuplex` is **on**, RX continues to run during TX.
  Full-duplex is the "monitor my own signal" / PureSignal capture
  configuration.
- The NB chain is toggled off during display-duplex transmit (lines
  29844 / 29878) to prevent transmit harmonics being chewed by NB.

---

## 3. PureSignal / full-duplex

### 3.1 What PureSignal needs from the protocol

**Confirmed**: PureSignal needs a feedback I/Q stream parallel to
regular RX.  Implementation visible in `console.cs::UpdateDDCs()`:

- **5-DDC radios (Orion class):** during TX with PS on, `P1_DDCConfig
  = 3`, `DDCEnable = DDC0 + DDC2`, `SyncEnable = DDC1`, sample rates of
  DDC0 and DDC1 forced to `cmaster.PSrate` (calibration-friendly rate,
  distinct from RX1's normal rate).  DDC0/DDC1 synced and used as the
  feedback-coupled pair; DDC2 carries on with normal RX1.
- **4-DDC radios (Hermes / Hermes-Lite / ANAN-10 / ANAN-100):** during
  TX with PS on, `P1_DDCConfig = 6`, `DDCEnable = DDC0`,
  `SyncEnable = DDC1`, both at `ps_rate` (or `rx1_rate` on HL2 since
  "HL2 can work at a high sample rate", explicit comment line 8476).
  DDC0 and DDC1 *both* point at the TX feedback path during transmit —
  RX1's normal listening is lost during key-down.  DDC0's frequency is
  force-set to `prn->tx[0].frequency` in `networkproto1.c` line 656
  when `(nddc == 2) && XmitBit && puresignal_run`.
- **2-DDC radios (ANAN-10E / 100B):** `P1_DDCConfig = 5`, `DDCEnable =
  DDC0`, `SyncEnable = DDC1`, `cntrl1 = 4`.  Same idea: both DDCs
  become the feedback pair while keying.
- Top-level `puresignal_run` flag set via `NetworkIO.SetPureSignal(1)`
  and high-priority packet bump (`SendHighPriority(1)`).  The flag is
  also stuffed into C2 of frame 11 (Preamp control) bit 6 and frame 16
  (BPF2) bit 6.

PureSignal at its core: "during TX, repurpose RX DDC(s) to sample the
feedback path, at a known sample rate, locked to the TX clock; the host
runs `iqc.c` / `calcc.c` to compute predistortion and pushes the
result to the TX path."

### 3.2 HL2 / HL2+ reality

`clsHardwareSpecific.cs` has a `PSDefaultPeak` entry for HermesLite
(0.233, both P1 and P2).  `UpdateDDCs` includes `HPSDRModel.HERMESLITE`
in the same branch as Hermes/ANAN-10/ANAN-100 with the explicit comment
"MI0BOT: HL2 can work at a high sample rate" (line 8476).  So Thetis
**treats HL2 as a PureSignal candidate**, same DDC-repurposing pattern
as Hermes.

What that does **not** tell us:

- Whether stock community gateware on a typical HL2 (Steve Haynal's
  `hermeslite2` repo) actually has the PureSignal-enabled DDC variant
  compiled in.  There is a "PureSignal" gateware variant discussed in
  the HL2 community; it is not the default.  Thetis assumes the
  gateware will obey the C&C frames, but if the gateware doesn't sample
  the TX feedback path, no amount of host toggling matters.
- Whether a hardware mod is needed.  Some HL2 builds need an
  antenna-tap / PA-output pickoff into the second ADC input to get a
  clean feedback signal.  Without this physical connection, predistor-
  tion has nothing to look at.

The MI0BOT HL2 Thetis fork ReleaseNotes do **not** mention PureSignal as
a working feature.  Honest read: **PureSignal on HL2 is plausibly
supported by Thetis but only with non-stock gateware and possibly a
hardware mod, and is not a baseline expectation.**  Lyra v0.0.9 should
not block on it.

### 3.3 Full-duplex vs the duplex bit

Two distinct things, even though both have "duplex" in the name:

- **Frame-0 C4 bit 2 ("duplex bit")**: a static bit that changes how
  the radio interprets frequency frames.  On HL2, this bit being set
  is a prerequisite for RX frequency updates to take effect at all.
  Thetis sets it unconditionally.  Lyra already sets it.
- **`chkFullDuplex` and `_display_duplex`**: an *operator* mode where
  RX continues to run while TX is keyed.  Enables monitoring your own
  signal, and what PureSignal effectively forces (you can't compute
  predistortion if the receiver shuts down during key-down).  On HL2
  with stock gateware, RX-during-TX may produce nothing useful (the
  antenna is connected to the PA output during TX, the receive
  front-end is muted) — but the protocol allows it, and it's
  independent of the duplex bit.

---

## 4. HL2 / HL2+ specifics

Picked up from the source tree:

- `HPSDRModel.HERMESLITE` is treated as a 4-DDC class in protocol setup
  (`P1_rxcount = 4`, `nddc = 4`), even though stock HL2 gateware only
  has 2 useful DDC slots.
- HL2 has a **separate read-loop** (`MetisReadThreadMainLoop_HL2`) that
  handles the I2C readback channel — when C0 has bit 7 set, frame data
  is interpreted as I2C response rather than ADC overload status.  HL2
  uses I2C extensively for I/O-board control (filters, antenna switch).
- HL2 has its own **write-loop** (`WriteMainLoop_HL2`) which schedules
  I2C writes inline with regular C&C frames, rotating between control
  frames and queued I2C writes (`prn->i2c.delay` mechanism).
- HL2 I/O Board (`IoBoardHl2.cs`) is a specific add-on with its own
  register set (frequency code bytes, antenna tuner, fan, ADC inputs,
  fault, op-mode); communicated via I2C bus 1 at address 0x1d.  Not
  all HL2 users have it.
- L/R audio channel order is swapped vs Anan: `NetworkIO.LRAudioSwap(1)`
  in HL2 branch of `clsHardwareSpecific.cs::Model.set`.
- Default PS peak for HL2 (`0.233`) much lower than Anan (`0.4`-ish) —
  different PA dynamic range.
- `HasVolts`/`HasAmps` includes HermesLite: HL2+ exposes supply-volt
  and current readings via AIN ADC channels; Thetis displays them.

The MI0BOT fork README confirms this is "the Thetis version for the
Hermes Lite 2"; it sits on top of the ramdor/Thetis codebase and adds
HL2-specific patches (read-loop, write-loop, I/O board).

---

## 5. Implications for Lyra

### `lyra/protocol/stream.py`

- Add a second 32-bit DDC1 frequency word writer using the C0=0x06 slot
  (frame index 3 in Thetis's rotation).  Fire whenever VFO B / RX2
  frequency changes, just like the existing RX1 frequency write.
- Frame-0 nddc field (`(nddc - 1) << 3` in C4 bits 6:3) currently sits
  at nddc=1; bumping to nddc=2 tells the FPGA to interleave a second
  DDC into EP6.  Match the field name in our code.
- The EP6 receive parser currently assumes `spr = 504 / (6*1 + 2) = 63`
  samples per DDC.  With two DDCs, `spr = 504 / (6*2 + 2) = 36` samples
  per DDC, and each sample slot is `[I0 Q0 I1 Q1]` of 12 bytes
  (3+3+3+3).  Mic still trails I/Q at offset `nddc * 6`, 2 bytes.
  **This is a substantial parser change, not a tweak.**
- `EnableRxs`-equivalent: also a "DDC enable bitmask" + "DDC sync
  bitmask" set via high-priority/setup frames (P2) or implicit nddc on
  P1.  On P1 HL2, setting nddc=2 + duplex bit is most of it, but
  `SetDDCRate(i, Rate[i])` per DDC matters for different rates per DDC.

### `lyra/radio.py` and `lyra/dsp/channel.py`

- Single `Radio` with one `Channel` becomes `Radio` with a list of
  channels.  Mirror Thetis's `dsp_rx[transceiver][subrx]` shape — even
  for v0.0.9 (main + RX2), leaving room for sub-receivers (multi-RX
  inside one DDC) is forward-compatible for SPLIT-without-RX2.
- DSP chain duplication: NR1 / NR2 / LMS / ANF / NB / AGC / Squelch
  each become per-channel.  Channel class is already isolated; just
  instantiate twice.
- DSP threading: a single network reader thread feeding two Channel
  chains is the simplest model, matches Thetis.  If our current arch
  has network reader and DSP on the same worker, keep that — DSP-per-RX
  is reasonable but harder, and Thetis does not do it.
- Sample rates per RX: keep the option open.  Thetis runs RX1 at e.g.
  192 kHz and RX2 at 48 kHz separately.  Probably not needed day one,
  but protocol supports it.

### Audio routing

Lyra's current AK4951 → line-in path is RX1-only by definition (one
stereo line).  Two real options:

- **Stereo split**: pan RX1 hard left, RX2 hard right, both into the
  same AK4951 stereo line.  Operator gets both RXs in headphones
  simultaneously; loses true stereo audio (most ops don't care for
  SSB/CW).
- **Second audio sink**: route RX2 to a host-side `sounddevice` output,
  while RX1 stays on AK4951.  Mirrors VAC2's "second virtual cable"
  idea.  Cleaner separation; needs a host-side output device choice.

Recommend implementing both, with **stereo-split as default**.  The
Thetis aamix mask pattern (bitmask of channels mixed into output) is a
clean abstraction worth porting in spirit.

### `TuningPanel`

- The dimmed RX2 freq display is already there; promote to live editing.
- Mode/Filter/AGC/NR controls assume one channel.  Most ergonomic
  answer: a "focused RX" concept where Mode/Filter binds to whichever
  RX has UI focus (Thetis does this implicitly via the active-receiver
  concept).  Alternative: doubled control strip like Thetis.  Doubled
  is more screen-real-estate-hungry but eliminates focus surprises.
  Pick the one matching Lyra's UI density.
- Add buttons: A→B, B→A, Swap, Split-toggle.  Cheap; logic is
  straightforward (Thetis lines 36272–36414).

### SPLIT and TX planning

- `chkVFOBTX` semantics are clearer if reified as "TX VFO source ∈
  {A, B}".  Then SPLIT is "TX-source = B while RX-source = A".
- Pile-up workflow needs operator to distinguish what RX1 and RX2 are
  doing.  On Thetis the visual answer: panadapter-top = RX1,
  panadapter-bottom = RX2, with a TX-frequency cursor drawn on
  whichever VFO TX will use.

### PureSignal posture for v0.0.9: **defer**

Lyra's TX path should be designed without assuming PureSignal
capability: linear PA, IQ-balance only, no predistortion.  Make the RX2
architecture clean enough that adding a "feedback DDC" mode later is
purely a protocol-mode change (set DDCEnable mask, repoint DDC0
frequency at TX freq during transmit, run a calibration loop), not a
Channel-class redesign.

**Critical**: do not wire DDC-frequency-source into a `rx2_freq` field;
abstract it as "this DDC's frequency source = {VFOA, VFOB, TX, custom}"
so the PS path is just an extra source.

Full-duplex monitoring (RX-during-TX without PureSignal) worth keeping
on v0.2 roadmap; much simpler than full PS, helps CW operators monitor
their keying without sidetone-only.

---

## 6. Open questions

Things not pinnable from the Thetis tree alone:

- **Exact HL2 gateware variant required for PureSignal.**  Thetis code
  paths are present, but the upstream Steve-Haynal `hermeslite2` repo
  has multiple gateware branches (`stable`, `develop`,
  `puresignal-experimental`-style names).  Confirm the exact variant
  before attempting PS testing on real hardware.
- **HL2 hardware tap requirement for PureSignal.**  Some HL2 PA modules
  expose a low-level coupled output; some require an external coupler
  tee'd into the antenna line.  Need to know which is true for the
  specific HL2+ board being used at N8SDR before promising PS support.
- **HL2 RX-during-TX behavior** with stock gateware.  Does the receive
  front-end actually mute during TX, or can it produce useful (but
  mismatched) data?  Empirical — keying into a dummy load with RX1
  running and looking at the panadapter will answer in five minutes.
- **Mic decimation factor on HL2.**  Thetis hard-codes mic decimation;
  value depends on host vs DDC sample rate.  Lyra-specific: HL2 RX
  audio currently goes through AK4951, not EP6 mic stream — but HL2
  still emits something on those mic bytes.  Confirm whether they're
  zero, line-in samples, or undefined, so the parser can drop them
  safely.
- **Sample-rate combinations supported on HL2.**  Thetis happily sets
  per-DDC rates, but real HL2 gateware quantizes to {48k, 96k, 192k,
  384k}.  Verify which combinations are stable on the specific HL2+
  board (some users report instability above 192k on certain firmware
  revisions).
- **Whether Lyra wants the "sub-receiver inside one DDC" mode** at all.
  Thetis has it as `chkEnableMultiRX`, costs nothing extra in protocol
  terms — but it's a third UI mode (RX2 disabled / multi-RX-in-one-DDC
  / RX2 enabled) that Lyra may want to skip in favor of the cleaner
  two-DDC story.

---

## Source files cited

All under `D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/Project Files/Source/`:

- `ChannelMaster/networkproto1.c` — protocol framing, EP6 parsing,
  C&C frame schedule
- `ChannelMaster/cmaster.c` — WDSP per-receiver setup loop
- `Console/console.cs::UpdateDDCs` — per-model DDC enable / sample-rate
  / PS combinations
- `Console/console.cs::UpdateAAudioMixerStates` — RX2 audio mix bitmask
- `Console/console.cs::btnVFOAtoB_Click / btnVFOBtoA_Click /
  btnVFOSwap_Click / chkVFOSplit_CheckedChanged` — VFO ergonomics
- `Console/console.cs::RX2Enabled` setter (~line 38077) — RX2 lifecycle
- `Console/radio.cs::Radio` and `RadioDSP` — DSP channel ID convention
- `Console/PSForm.cs::PSEnabled` setter — top-level PureSignal entry
- `Console/clsHardwareSpecific.cs::Model.set / PSDefaultPeak` — HL2
  model wiring
- `Console/HPSDR/IoBoardHl2.cs` — HL2 I/O board register map (context)
- `Console/HPSDR/NetworkIO.cs` — `VFOfreq`, model-specific board checks

Thetis is GPL v2+; Lyra is GPL v3+; license-compatible if we ever did
want to port code (we don't, but worth noting).
