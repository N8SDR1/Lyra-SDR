# HL2 PureSignal + audio chain — Thetis deep dive

Reference notes from a focused read of the openHPSDR Thetis 2.10.3.13
source tree at `D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/Project Files/
Source/`, specifically targeting how Thetis handles PureSignal and
audio routing **for the Hermes Lite 2 / 2+**.

Supersedes parts of `rx2_research_notes.md` — that earlier pass treated
HL2 PureSignal as gateware-uncertain.  Per project owner (N8SDR, who
is running PureSignal on an HL2/HL2+ via Thetis on the air right now),
PureSignal works on HL2 with the appropriate gateware variant + an
internal hardware mod that re-introduces the PA sampler output to the
HL2 ADC.

**No code copied** — pattern descriptions and file/line citations only.

---

## 1. PureSignal on HL2 — how it actually works

### 1.1 Big picture: HL2 is a 4-DDC radio in protocol terms

Thetis groups the HL2 with Hermes / ANAN-10 / ANAN-100 — **not** with
the 2-DDC ANAN-10E / 100B — for its DDC topology.  This is the single
most important architectural fact and the previous research pass got it
wrong.

In `Console/console.cs::UpdateDDCs()` (~lines 8408–8490), the case
statement for `HPSDRModel.HERMES | HERMESLITE | ANAN10 | ANAN100` sets:

- `P1_rxcount = 4` ("RX4 used for puresignal feedback")
- `nddc = 4` (number of DDCs the FPGA streams)
- During TX with PS engaged: `DDCEnable = DDC0`, `SyncEnable = DDC1`,
  `cntrl1 = 4`, `cntrl2 = 0`

The 2-DDC HL2 silicon therefore appears to Thetis as a **4-DDC P1
endpoint**.  Implemented in HL2 gateware: the gateware exposes 4
logical DDCs, even though only 2 physical DDC engines exist.  DDC2 and
DDC3 are the "extra" pair that get repurposed for PS feedback.

### 1.2 Wire-level mechanism: `puresignal_run` propagation

The flag travels host→radio via two C&C frames in the EP2 stream
(`ChannelMaster/networkproto1.c::WriteMainLoop_HL2`, lines 869–1201):

- **Frame index 11** (C0 = 0x14): `C2 |= (puresignal_run & 1) << 6`
  (line 1097)
- **Frame index 16** (C0 = 0x24, "BPF2"): `C2 |= (puresignal_run & 1)
  << 6` (line 1157)

Both PS bits sent every C&C round-trip; gateware uses them to gate the
feedback ADC path.  Thetis itself never reads PS status back from the
HL2 — sets the bit, trusts the gateware.

### 1.3 DDC frequency assignment with PS engaged

The HL2 path uses `nddc==4`, so the `nddc==2 && puresignal_run`
conditions in `WriteMainLoop_HL2` (lines 985, 1000) **do not fire** —
those are Hermes-II-only special cases.  Instead, HL2 follows the
static `nddc==4` DDC-frequency map:

| C&C frame | DDC | Purpose during RX | Purpose during TX+PS |
|---|---|---|---|
| 2 (C0=0x04) | DDC0 | RX1 freq | RX1 freq (unchanged) |
| 3 (C0=0x06) | DDC1 | RX2 freq | RX2 freq (unchanged) |
| 5 (C0=0x08) | DDC2 | TX freq | TX freq (always) |
| 6 (C0=0x0a) | DDC3 | TX freq | TX freq (always) |

**During PS+TX**: DDC0/DDC1 keep producing receive samples (whatever
the gateware does with them when PS is set), and DDC2/DDC3 are tuned
to TX frequency to capture the PA-output sample stream.

In `MetisReadThreadMainLoop_HL2` (lines 422–586), with `nddc==4`:

- `xrouter(0,0,0, RxBuff[0])` → DDC0 → RX1 stream
- `twist(spr, 2, 3, 1)` → DDC2+DDC3 interleaved → stream 1, the PS
  feedback stream
- `xrouter(0,0,2, RxBuff[1])` → DDC1 → RX2 stream

Operational model: **DDC0=RX1, DDC1=RX2, DDC2+DDC3=PS feedback
(interleaved as one logical stream feeding WDSP's PS calibration
channel)**.

### 1.4 PSrate — HL2 special case

`Console/cmaster.cs` lines 424–437: `ps_rate = 192000` (default).  On
most ANAN radios, `Rate[0]=Rate[1]=ps_rate` during PS+TX.

**HL2 takes a different path** at `console.cs::UpdateDDCs` lines
8474–8485:

```
if (hpsdr_model == HPSDRModel.HERMESLITE) {
    Rate[0] = rx1_rate;
    Rate[1] = rx1_rate;
} else {
    Rate[0] = ps_rate;   // 192000
    Rate[1] = ps_rate;
}
```

Source comment: "HL2 can work at a high sample rate."  On HL2, the
RX1/RX2 DDC rates **stay at the user-selected RX rate during PS+TX**
(probably because DDC2/DDC3 carry the feedback at FPGA-internal rate;
the host gets feedback samples at whatever rate the gateware delivers
DDC2/3 — most likely matched to `rx1_rate`).  The DLL call
`puresignal.SetPSFeedbackRate(txch, ps_rate)` (cmaster.cs:435) is still
made when `PSrate` is written, telling WDSP what rate to expect on its
PS RX side.

### 1.5 PS calibration loop and operator UX

Source: `Console/PSForm.cs` and WDSP exports in `wdsp/calcc.c::
SetPSRunCal`, plus the DllImports listed at `PSForm.cs` lines
1014–1072.

State machine in `PSForm.cs::cmdState` enum (lines 113–117) and
`timer1code` (~line 633):

- `OFF` → operator clicks PS → `TurnOnAutoCalibrate` →
  `AutoCalibrate` (continuous)
- Operator clicks "Single Cal" (`btnPSCalibrate_Click`, line 466) →
  `_singlecalON=true` → `TurnOnSingleCalibrate` → `SingleCalibrate` →
  returns to `AutoCalibrate` once correction lands
- "Two-Tone" (`btnPSTwoToneGen_Click`, line 508) injects two-tone test
  signal into TX chain

`SetPSControl(channel, reset, mancal, automode, turnon)` is the central
WDSP entry point.  Combinations:

- `(1, 0, 0, 0)` = reset
- `(0, 1, 0, 0)` = manual single cal
- `(0, 0, 1, 0)` = automode
- `(0, 0, 0, 1)` = restore-corrections

### 1.6 Auto-attenuation differences for HL2

`PSForm.cs::NeedToRecalibrate_HL2` (line 1142) and `timer2code` (~line
728+):

- HL2 attenuator range: **-28 to +31 dB** (line 4003 in setup.cs sets
  `udATTOnTX.Minimum = -28`); standard ANAN range is `0..31`.
- HL2-specific recalibrate trigger: `FeedbackLevel > 181 ||
  (FeedbackLevel <= 128 && nCurrentATTonTX > -28)` versus the ANAN
  range elsewhere.
- Different `IsFeedbackLevelOK` clamping for HL2 (lines 758–768): HL2
  uses `ddB = 10.0` defaults; standard radios use `ddB = 31.1`.

PSForm ships the same UI for HL2; auto-attenuate logic just has its
own bounds.

### 1.7 The hardware mod — Thetis is silent

Thetis source contains **zero** references to a specific HL2 hardware
mod, sampler tap point, or required test point.  No auto-detection;
Thetis simply sets `puresignal_run=1` and trusts the gateware.

The setup-form HL2 group has only `chkHL2BandVolts` (controls ADC
dither bit) and `chkHL2PsSync` ("Power supply sync" — controls ADC
random bit, NOT PureSignal sync; setup.cs line 13385).  Neither has
anything to do with PureSignal.

This means: **the PS-mod requirement and the gateware variant
supporting it are entirely external to Thetis.**  Thetis's contract is
"I will set the bit; you (gateware + hardware) make feedback samples
appear in DDC2+DDC3 at TX frequency."  Whether that works is a
hardware/gateware truth on the operator's bench, not something Thetis
can confirm or deny.

Per N8SDR: the mod re-introduces the PA sampler output to the HL2 ADC
(the HL2's ADC time-shares between antenna RX and PA feedback under
FPGA control, gated by `puresignal_run`).  The gateware that supports
this is an HL2-specific variant maintained in the HL2 community.

### 1.8 Gateware variant

No reference to a specific gateware version in the Thetis source.
Operator's responsibility: load HL2 gateware that exposes 4 DDCs and
routes the PA-sample feedback into DDC2/DDC3 when `puresignal_run` is
asserted.  Per N8SDR, this works in real deployments.

---

## 2. HL2 audio chain in Thetis

### 2.1 EP2 frame layout (host→radio audio)

`ChannelMaster/networkproto1.c::sendProtocol1Samples` (lines
1204–1267) and the assembly into `OutBufp`:

- **Per-USB-frame**: 63 LRIQ samples × 8 bytes = 504 bytes payload +
  8-byte CC header = 512 bytes; two frames per UDP datagram.
- **Sample layout per LRIQ tuple** (8 bytes):
  `[L_msb L_lsb] [R_msb R_lsb] [I_msb I_lsb] [Q_msb Q_lsb]`  —
  big-endian 16-bit signed.
- `pbuffs[0] = outLRbufp` (RX audio, L+R, double-precision pre-
  quantization)
- `pbuffs[1] = outIQbufp` (TX I+Q from the WDSP transmitter)

### 2.2 Stereo handling for RX2

The L/R audio in `outLRbufp` is fully mixed by WDSP `aamix` in
ChannelMaster before reaching the EP2 packer.  The split into L vs R
is done **inside WDSP** by the per-DSPRX `Pan` parameter:

- `console.cs::ptbRX2Pan_Scroll` (line 39495):
  `radio.GetDSPRX(1, 0).Pan = val;` where `val ∈ [0,1]` (0=full left,
  1=full right, 0.5=center).
- **Default Pan = 50/100 = 0.5 (centered → mono mix)**.  Operator
  stereo-split (RX1 left ear, RX2 right ear) requires explicitly
  setting `RX1Pan=0` and `RX2Pan=1`.
- AAMix routing for HL2 in `UpdateAAudioMixerStates` (console.cs lines
  28240–28252 for USB protocol; 28255+ for ETH): mixer states
  explicitly include `RX1+RX1S+RX2+MON` for both VAC and audio output
  devices when RX2 is enabled.

So **HL2 sends a single stereo audio stream over EP2; the L/R channels
are whatever WDSP mixed with the per-RX pan settings**.  RX2 stereo
split is "set RX1Pan=0, RX2Pan=1, then both arrive in the EP2 LR
bytes."  Nothing HL2-specific in the path, but Thetis defaults to
centered, so operator must pan manually.

**For Lyra: stereo split should be the auto-applied default when RX2
is enabled, not require operator action.**

### 2.3 The L/R swap quirk

`network.h::swap_audio_channels` (line 110), `networkproto1.c` lines
1231–1238: optional bit that swaps L↔R before packing into EP2.
ReleaseNotes 2.10.3.13 Beta 2: "Added ability to swap audio channels
sent to hardware (Not tested)."

**Different HL2 firmware revs evidently swap L and R; Thetis added an
option to compensate.  Lyra needs the same compensation knob.**

### 2.4 AK4951 codec: not configured by Thetis

Thetis never writes AK4951 registers.  HL2 gateware initializes the
codec internally, fixed to 48 kHz.  Thetis's I2C bus interface
(`ChannelMaster/netInterface.c::I2CWrite/I2CReadInitiate`, lines
1470–1599) is used only for the optional N7DDC IO-board
(`Console/HPSDR/IoBoardHl2.cs`, addresses 0x1d and 0x41 on bus 1) —
the auto-tuner.

Volume/mute is implemented in software on the host inside WDSP
(`SetAAudioMixVol`, etc.); the codec just plays whatever PCM is in the
EP2 LR bytes.

### 2.5 PC sound card vs hardware-audio path

These coexist: WDSP produces a single audio output that gets split
into:

1. **EP2 LR bytes → HL2 codec → headphone jack** (always, if HL2 model
   is selected)
2. **PC sound device** (configured in Audio setup) — runs in parallel
   via cmasio / audio.cs

There is no "select one or the other."  PSEnabled does NOT change this
routing.  HL2 mod for PS doesn't touch audio either way.

### 2.6 Latency

HL2-specific TX-latency control: C&C frame index 17 (`WriteMainLoop_
HL2`, line 1162): `C0=0x2e, C3=ptt_hang, C4=tx_latency`.  Thetis tells
the HL2 how many samples to buffer between EP2 receipt and DAC output
(`NetworkIOImports.cs:384`: `SetTxLatency`, `SetPttHang`).  PC-
soundcard path has its own buffer sliders independent of this.

### 2.7 Mic input

EP6 high-priority frames carry mic samples encoded into the same data
stream as the IQ samples.  Read loop `MetisReadThreadMainLoop_HL2`
(lines 470–579): for each USB frame, after the per-DDC IQ samples, the
mic sample is extracted at offset `8 + nddc*6 + isamp*(2 + nddc*6)` as
a 16-bit big-endian signed int (lines 564–576).  Sent via
`Inbound(inid(1, 0), ...)` into the WDSP TXA chain.

CW sidetone/dot/dash bits get OR'd into the bytes when in CW mode.
**HL2 special**: bit 3 carries CWX PTT (lines 1248–1252 in
networkproto1.c).

---

## 3. Lyra v0.0.8 implications

### 3.1 RX2 protocol

- `protocol/stream.py`: when RX2 is enabled, set **`nddc=4`** (not 2!).
  Matches `console.cs::UpdateDDCs` HL2 case.
- DDC0=RX1, DDC1=RX2, DDC2/DDC3=available for PS in v0.1+.
- C4 byte in C&C frame 0 must include `(nddc-1)<<3 = 0x18`.
- Duplex bit `C4 |= 0x04` mandatory regardless (HL2 quirk we already
  honor).
- C&C frame indices 2 and 3 (DDC0/DDC1 freq) carry RX1 and RX2
  frequencies.  Frames 5 and 6 (DDC2/DDC3 freq) carry TX freq, set
  always — if not transmitting, harmless; if transmitting with PS,
  these are the feedback freqs.
- Read-side: HL2 read frame is 504 bytes payload, `spr = 504/(6*nddc +
  2) = 504/26 ≈ 19` samples per DDC per USB frame at nddc=4.  Each
  sample is 24-bit big-endian I/Q × 2; mic sample at end of each tuple
  (2 bytes).  **EP6 parser rewrites for nddc=4 layout.**

### 3.2 Audio routing for RX2 stereo split

- WDSP-equivalent per-RX pan: Lyra needs a `pan ∈ [0,1]` per RX with
  default 0.5.  RX2-stereo-split UX = "RX1.pan=0, RX2.pan=1."  Mixed
  audio goes into a single L/R stream that gets quantized into EP2 LR
  bytes.
- EP2 LR encoding: 16-bit big-endian signed, per-tuple `[L_msb L_lsb
  R_msb R_lsb I_msb I_lsb Q_msb Q_lsb]`, 63 tuples per USB frame, 2 USB
  frames per UDP datagram.  Lyra can probably reuse the existing v0.0.7
  EP2 packer; ensure the LR slot is mixed-WDSP-output-with-pan rather
  than RX1-only.
- **Add a `swap_lr_audio` Settings option** for firmware-rev
  compensation.  Mirrors `swap_audio_channels` in network.h:110.
- **Default behavior when RX2 enabled**: auto-set RX1.pan=0, RX2.pan=1
  (full stereo split).  Operator can adjust per-RX pan in Settings if
  they want a different mix.

### 3.3 Forward-compat hooks for v0.1+ PureSignal

If Lyra v0.0.8 abstracts:

- **DDC frequency source per-DDC** (currently RX1 freq, RX2 freq,
  optional TX freq for DDC2/DDC3) — that's the only thing PS needs at
  the wire level.
- **`puresignal_run` boolean** that toggles bit 6 in C&C frames 11 and
  16 (`(0x14, C2)` and `(0x24, C2)`).
- **Full-duplex bit always set** (already required for HL2 RX during TX
  anyway — see `C4 |= 0x04` in `WriteMainLoop_HL2` line 967).

…then **v0.1 PS becomes mostly a UI + DSP problem** (predistortion
math + calibration loop), not a protocol problem.  Lyra should plumb
these now even though they're inert in v0.0.8.

### 3.4 v0.0.9 TX implications

- Mic sample is in the same EP6/return frame stream as IQ — Lyra's RX
  path already gets it for free.  TX path needs the `outIQbufp`
  packing done in `sendProtocol1Samples`.
- HL2 CWX PTT uses bit 3 in the I-sample LSB during CW
  (networkproto1.c:1249) — HL2-specific quirk.
- TX latency / PTT hang: write C&C frame 17 (`C0=0x2e`); HL2-specific
  knobs that standard HPSDR P1 doesn't have.

---

## 4. Files in Thetis to reference when implementing each Lyra piece

| Lyra concern | Thetis file(s) | Section |
|---|---|---|
| HL2 P1 read loop | `ChannelMaster/networkproto1.c` lines 422–586 | `MetisReadThreadMainLoop_HL2` |
| HL2 P1 write loop | `ChannelMaster/networkproto1.c` lines 869–1201 | `WriteMainLoop_HL2` |
| HL2 EP2 audio quantization | `ChannelMaster/networkproto1.c` lines 1204–1267 | `sendProtocol1Samples` |
| DDC topology decisions | `Console/console.cs` lines 8214–8577 | `UpdateDDCs` |
| AAMix routing for HL2 | `Console/console.cs` lines 28217–28333 | `UpdateAAudioMixerStates` |
| PSrate plumbing | `Console/cmaster.cs` lines 424–437 | `PSrate` setter |
| PS state machine | `Console/PSForm.cs` lines 553–727 | `timer1code` |
| PS auto-attenuate (HL2 special) | `Console/PSForm.cs` lines 728–820, 1142–1145 | `timer2code`, `NeedToRecalibrate_HL2` |
| PS WDSP API surface | `Console/PSForm.cs` lines 1014–1072 | DllImports |
| HL2 TX latency / PTT hang | `Console/HPSDR/NetworkIOImports.cs` lines 384–387; `networkproto1.c` lines 1162–1168 | C&C frame 17 |
| HL2 I2C bus (IO-board only) | `ChannelMaster/netInterface.c` lines 1470–1599; `Console/HPSDR/IoBoardHl2.cs` | I2CRead/Write |
| HL2 model enum | `ChannelMaster/network.h` line 475 | `HPSDRModel_HERMESLITE = 14` |
| HL2-specific attenuator ranges | `Console/console.cs` lines 2098–2110, 11041 | `tx_attenuator` & LNA |
| HL2 audio L/R swap | `ChannelMaster/network.h:110`; `networkproto1.c:1231` | `swap_audio_channels` |
| `puresignal_run` flag | `ChannelMaster/network.h:151`; `netInterface.c` lines 838–840 | setter |
| HL2 temperature/PA-current readouts | `Console/console.cs` lines 24914+, 25537+, 25552+ | `computeHermesLiteTemp`, `computeHermesLitePAAmps` |

---

## 5. Open questions

These are gaps in the Thetis source that Lyra cannot answer from code
alone — empirical testing on N8SDR's bench or community references:

1. **Does the HL2 gateware actually deliver TX-frequency samples on
   DDC2/DDC3 during PS+TX?**  Thetis assumes yes; verify by capturing
   UDP packets during PS TX and checking DDC2/DDC3 stream content vs
   DDC0/DDC1.
2. **What HL2 gateware version supports PureSignal-compatible 4-DDC?**
   Thetis source has no version check.  Likely a community-maintained
   variant (probably named in HL2 build tags or wiki).  N8SDR is
   running such gateware now — should confirm the exact version.
3. **What is the exact PS-mod tap point?**  Likely the PA output via a
   small attenuator into the HL2's dedicated ADC RX-feedback input
   (single ADC time-shared with antenna).  Not in Thetis source;
   community schematic references would clarify.
4. **What DDC sample rate do DDC2/DDC3 deliver during PS+TX on HL2?**
   Thetis sets `Rate[0]=Rate[1]=rx1_rate` for HL2 (not `ps_rate=192000`
   like ANAN), implying gateware delivers feedback at user RX rate.
   Worth confirming on a pcap.
5. **HL2+ vs HL2 differences**: Thetis source uses one model code
   (`HERMESLITE`) for both.  Differences (if any) must be detectable
   from gateware/discovery response, not from Thetis.  Need to check
   what HL2+ reports in its discovery packet.
6. **AK4951 mic gain vs `mic_boost` vs `line_in_gain`**: Thetis writes
   these into C&C frame 11 but HL2 gateware re-interprets them for the
   AK4951.  The mapping (gateware-side) isn't in Thetis.  Empirical
   testing if Lyra wants per-step calibration.
7. **The "PsSync" checkbox actually doesn't engage PureSignal**: it
   just flips the ADC random bit.  If a real "PS hardware mod present"
   toggle should appear in Lyra's UI, it does not exist in Thetis.
   Lyra may need to add one and let the operator self-attest.

---

## 6. TL;DR

PureSignal on HL2 is wired into Thetis as a 4-DDC topology where DDC2
and DDC3 get repurposed to TX frequency during transmit, providing a
feedback sample stream that WDSP's calcc/iqc consumes for
predistortion.  Thetis sets one bit (`puresignal_run`) in two C&C
frames, sets `nddc=4`, sets the duplex bit, and trusts the gateware to
deliver PA feedback samples.

There is no Thetis-side hardware mod detection or gateware version
check.  The only HL2-specific PS code is the PSForm auto-attenuate
clamps (-28..+31 dB range and the recalibrate threshold).

For Lyra: RX2 (v0.0.8) is straightforward — use **`nddc=4`** (not 2),
plumb DDC0/DDC1 to RX1/RX2 freqs, route audio through a per-RX `pan`
knob into the standard EP2 LR bytes — and the architectural setup
naturally supports v0.1 PureSignal as a flag-flip plus DSP work, no
protocol redesign needed.
