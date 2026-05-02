# Lyra HL2/HL2+ implementation playbook: RX2, TX, PureSignal

Senior-engineering deep dive across Thetis 2.10.3.13 and WDSP source
trees at `D:\sdrprojects\OpenHPSDR-Thetis-2.10.3.13\Project Files\
Source\`.  Goal: concrete implementation plan for Lyra v0.0.9 (RX2),
v0.1 (TX), and v0.2 (PureSignal), targeting **Hermes Lite 2 / 2+
only**.

License posture: Lyra is GPL v3+.  WDSP is GPL v3+ (post Pratt
relicense).  Therefore:

- **Port from WDSP directly** when the algorithm is non-trivial and
  Lyra would otherwise reinvent it (with attribution).
- **Write Lyra-native, modeled on Thetis pattern** for protocol /
  audio-routing / UI glue (Thetis is GPL v2+ / Microsoft.NET, not
  helpful to copy directly).

This playbook supersedes parts of `rx2_research_notes.md` and
`hl2_puresignal_audio_research.md` — corrections in §2 below.

---

## 1. Executive summary

The three-release roadmap (RX2 in v0.0.9, TX in v0.1, PureSignal in
v0.2) is structurally sound.  The prior-pass research notes are mostly
correct; the three corrections in §2 are minor.

**Top-level recommendation per topic:**

- **RX2 (v0.0.9): pure Lyra-native, no WDSP port required.**  Lyra
  already has Python-native ports of every per-channel DSP block
  needed (NR1/NR2/LMS/ANF/NB/AGC/SQ in `lyra/dsp/`).  The work is (a)
  making the protocol layer multi-DDC-aware at `nddc=4`, (b)
  duplicating the `Channel` instance, and (c) writing a tiny stereo
  mixer (~80 LOC of NumPy) that replaces `aamix.c`.  Don't port
  `aamix.c`; it's a thread-bound C shim around a one-line
  vector-multiply-accumulate.  Just write the multiply-and-accumulate
  in NumPy in `lyra/dsp/mix.py`.

- **TX (v0.1): hybrid — Lyra-native protocol + PTT state machine,
  but the TXA DSP chain (mic processing → modulator → IF filtering →
  ALC → I/Q out) is too large to port to Python in a useful
  timeframe.**  Recommendation: build a minimum-viable TX in pure
  Python first (mic → bandpass → SSB modulator → I/Q clip → I/Q
  frame), defer compressor / leveler / CFC / preemph / EER to a later
  v0.1.x once we know the field-tested Python implementation hits
  no CPU walls.  Don't reach for cffi/WDSP DLL until profiling forces
  it; the Hermes-Lite TX I/Q rate is 48 kHz on a Python NumPy budget
  that's roughly 50× the per-sample cost we can absorb.

- **PureSignal (v0.2): the only path that genuinely benefits from
  porting WDSP C → Python.**  Port `calcc.c` and `iqc.c` directly —
  these files are 1164 + 315 = ~1500 lines of C, but the math is a
  finite-set: pair-binning, complex-envelope distortion fitting,
  cubic-spline coefficient builder (`xbuilder` from `lmath.c`), and a
  Hann-windowed swap/begin/end coefficient loader.  Estimated 2–3
  weeks Python port.  The protocol surface is already plumbed by
  v0.0.9 (`puresignal_run`, DDC2/DDC3 freq-source = TX).  Operator UX
  is a port of `PSForm.cs` to a Lyra-native `PSDialog` — modeled on
  the pattern, written from scratch.

**Threading**: Lyra's "one network reader thread, all DSP on one
worker" matches Thetis's pattern.  Don't change it for v0.0.9.  PS in
v0.2 may want a separate thread for `calc()` (the slow predistortion-
coefficient builder) — that's the **only** new thread the three
releases require beyond what Lyra has today.

---

## 2. Corrections to prior research passes

### 2.1 The "DDC frequency source" abstraction needs one tweak

The prior-pass plan had:

```
ddc[0].freq_source = "VFOA"  # RX1
ddc[1].freq_source = "VFOB"  # RX2
ddc[2].freq_source = "TX"    # PS feedback
ddc[3].freq_source = "TX"    # PS feedback
```

Verified against `networkproto1.c::WriteMainLoop_HL2` lines 982–1043.
**However**: with `nddc==4` the prior-pass concern that "DDC2/DDC3
always TX freq might cause unwanted DDC2/DDC3 traffic" is real.
`MetisReadThreadMainLoop_HL2` at line 549–553:

```c
case 4:
    xrouter(0, 0, 0, spr, prn->RxBuff[0]);   // DDC0 → stream
    twist(spr, 2, 3, 1);                      // DDC2+DDC3 interleaved → stream 1
    xrouter(0, 0, 2, spr, prn->RxBuff[1]);   // DDC1 → stream
```

DDC2/DDC3 samples are **always** delivered when `nddc==4`.  Lyra's
parser must accept and discard them when PS is off.  The existing-plan
note "DDC2/DDC3 samples are quietly dropped in v0.0.9 (PS not
engaged)" is correct but understated — these aren't optional bytes;
they take their slots in the 26-byte cadence and Lyra's parser must
skip them.

### 2.2 The 26-byte slot is correct; double-check the "I before Q" ordering

Prior pass said: 26 bytes per slot at `nddc=4`, layout
`[I0 Q0 I1 Q1 I2 Q2 I3 Q3 mic]`.  Confirmed —
`MetisReadThreadMainLoop_HL2` line 532:

```c
int k = 8 + isample * (6 * nddc + 2) + iddc * 6;
```

So for sample `isample`, DDC `iddc`'s bytes start at offset
`8 + isample*26 + iddc*6`.  Within those 6 bytes, lines 533–540:
I-bytes 0..2 first (big-endian 24-bit signed), then Q-bytes 3..5.
Mic (2 bytes BE signed 16-bit) at offset
`8 + nddc*6 + isample*26` per line 564.  **No swap, no I/Q
transposition.**

Lyra's existing `_decode_iq_samples` works on the `nddc==1` 8-byte
layout.  For nddc=4 it needs to be rewritten — not patched.  Suggest
a new function `_decode_nddc_block(block_bytes, nddc) -> dict[int,
np.ndarray]` returning a dict keyed by ddc index → complex64 samples,
plus a separate mic ndarray.

### 2.3 WDSP's pan curve isn't linear — it's a sin-cosine "boost" pan law

Prior pass said: "RX1.Pan=0, RX2.Pan=1 = hard left/right."  This is
correct, but the curve between the endpoints is **not linear**.  Read
`wdsp/patchpanel.c::SetRXAPanelPan` lines 158–176:

```
if pan <= 0.5: gainL = 1.0,         gainR = sin(pan·π)
if pan  > 0.5: gainL = sin(pan·π),  gainR = 1.0
```

At pan=0 → (1, 0) hard left.  At pan=1 → (0, 1) hard right.
**At pan=0.5 → (1, 1)** — both channels at unity, which is louder
than either endpoint by 6 dB.  This is a "constant-amplitude-on-the-
strong-side" rule, not Lyra's existing equal-power rule (cos/sin at
π/4 = 0.707 each at center).

For Lyra: the **prior pass's stereo-split spec is fine** because both
endpoints are still (1,0) / (0,1).  But if operators ever sit at
intermediate pan values, the perceived loudness differs from Lyra's
existing Balance.  **Recommendation**: in Lyra's per-channel `pan`
parameter (new in v0.0.9), use the WDSP curve verbatim — Thetis users
will recognize the behavior and Lyra's existing Balance can be
reframed as "pan slider" cleanly.  Cite `wdsp/patchpanel.c:158–176`
in the port-attribution comment.

### 2.4 The Hermes II `nddc==2` PS path has no relevance to HL2

Prior pass correctly identified this in `hl2_puresignal_audio_research
.md` §1.3 but the wording was muddled.  Restating cleanly: HL2 always
uses `nddc==4`.  The conditional `(nddc == 2) && XmitBit &&
puresignal_run` branches in `WriteMainLoop_HL2` at lines 985 and 1000
are **Hermes-II only**.  Lyra's protocol layer can hardcode `nddc=4`
for HL2 with no special-cases — both during normal RX and during PS+TX.
Frame-3 (DDC1) always carries RX2 freq, never TX freq — the `nddc==2`
PS path is dead code on HL2.

### 2.5 The "DDC2/DDC3 freq during normal RX" question is empirical

Prior research left this open: "with PS off, do DDC2/DDC3 samples end
up at the TX freq, and does that cause issues?"  Read of
`networkproto1.c` lines 1023–1043 confirms DDC2 (frame index 5) and
DDC3 (frame index 6) **always** get loaded with `prn->tx[0].
frequency`, regardless of PS state.  Whether the gateware actually
opens those DDCs to produce useful samples is gateware-dependent.
The protocol cost is zero; it's just an extra freq write per
round-robin cycle.

**Recommendation**: in Lyra v0.0.9, write DDC2/DDC3 freq = current
TX-VFO freq (which equals VFOA when SPLIT is off, VFOB when SPLIT is
on).  The samples are dropped at the parser.  When v0.2 lands and we
set `puresignal_run=True`, the same freq writes become "PS feedback
freq is TX freq" — no additional protocol work needed.

---

## 3. RX2 implementation playbook (v0.0.9)

### 3.1 Module-by-module decisions

| Concern | Approach | Lyra path | Source pattern |
|---|---|---|---|
| EP6 multi-DDC parser | Lyra-native rewrite | `lyra/protocol/stream.py` | `networkproto1.c::MetisReadThreadMainLoop_HL2` lines 527–559 (study, don't copy) |
| C&C frame-0 nddc field | Lyra-native edit | `lyra/protocol/stream.py` `_config_c4` | `networkproto1.c:968` |
| Per-DDC freq writers (frames 2/3/5/6) | Lyra-native | `lyra/protocol/stream.py` round-robin table | `networkproto1.c:982–1043` |
| Per-RX DSP chain | Already exists, instantiate twice | `lyra/dsp/channel.py` Channel class | n/a (Lyra-native) |
| Per-RX pan logic | Port from WDSP | `lyra/dsp/mix.py` (new) | `wdsp/patchpanel.c::SetRXAPanelPan` lines 158–176 |
| L+R bus mixer | Lyra-native, ~30 LOC NumPy | `lyra/dsp/mix.py` (new) | aamix.c L 425–458 (study only — it's just a sum) |
| Audio sink (AK4951) | Already exists | `lyra/dsp/audio_sink.py::AK4951Sink` | n/a |
| L/R swap option | Lyra-native, 3-line check | `lyra/dsp/mix.py` | `networkproto1.c:1231–1238` |

### 3.2 Protocol bits and bytes

The exact byte-level changes in `lyra/protocol/stream.py`:

**Frame 0 (C0=0x00) C4 byte construction**:

- bits[1:0] = antenna select (HL2 uses 00 — no relevance)
- bit 2 = duplex bit, **always 1** (already set in current Lyra)
- bits[6:3] = `nddc - 1`.  For HL2 with RX2, write
  `(4-1) << 3 = 0x18`.  Combined with duplex: `c4 = 0x1C` when
  RX2-capable, `0x04` when single-RX legacy mode.
- bit 7 = diversity (HL2: 0)

**Round-robin C&C register table (in `self._cc_registers`)** —
extend Lyra's existing dict:

```
0x00: (sr_code, 0x00, 0x00, 0x1C)             # general, c4=0x1C with nddc=4
0x02: TX freq (4 bytes, big-endian)            # TX VFO
0x04: DDC0 freq = RX1 / VFOA                   # RX1
0x06: DDC1 freq = RX2 / VFOB                   # RX2  (new in v0.0.9)
0x08: DDC2 freq = TX freq                      # static TX, harmless if PS off (new)
0x0A: DDC3 freq = TX freq                      # static TX, harmless if PS off (new)
0x0E: ADC assignments + tx_step_attn           # frame 4
0x14: preamps + line-in + puresignal_run<<6 + ATT  # frame 11 (new — puresignal bit)
0x16: step ATT control                         # frame 12 (TX-time check)
0x1E: CW config                                # frame 13
0x20: CW hang/sidetone                         # frame 14
0x22: EER PWM                                  # frame 15
0x24: BPF2 + xvtr_en + puresignal_run<<6       # frame 16 (new — puresignal bit)
0x2E: tx_latency + ptt_hang                    # frame 17 (HL2-specific, defer to v0.1)
```

The current Lyra round-robin steps through the dict each frame.
Adding entries here costs zero per-frame budget.  The **first time**
a freq changes, set the dict entry **and** push a one-shot send
(already the pattern in `_set_rx1_freq`).

**EP6 parser rewrite**:

```python
def _decode_nddc4_block(block: bytes) -> tuple[bytes, dict[int, np.ndarray], np.ndarray]:
    """Parse one 512-byte USB block at nddc=4: returns (cc, {ddc_idx: iq}, mic).

    Layout:
      bytes [0:3]    = 0x7F sync
      bytes [3:8]    = C0..C4
      bytes [8:512]  = 504 bytes = 19 sample-slots × 26 bytes/slot
        per-slot: I0(3)|Q0(3)|I1(3)|Q1(3)|I2(3)|Q2(3)|I3(3)|Q3(3) | mic_hi(1)|mic_lo(1)
    """
```

Implementation: vectorize as `np.frombuffer(block[8:],
dtype=np.uint8).reshape(19, 26)`.  For each DDC, slice columns
`[iddc*6 : iddc*6+6]` → assemble big-endian 24-bit ints with the same
shift-and-sign-extend pattern Lyra already uses.  Mic from columns
`[24:26]` → BE int16.  Return all four DDC streams; caller routes.

**Routing**: extend `HL2Stream.start(on_samples=...)` to a multi-
callback API.  Keep backward compat by keeping `on_samples` for DDC0
and adding `on_rx2_samples=None`, `on_ps_samples=None` keyword args.
When `on_rx2_samples` is None (single-RX mode), still call the
parser, just discard DDC1 output.  Cleaner alternative: introduce a
single `on_ddc_samples(ddc_idx, samples, stats)` callback and let
Radio dispatch — recommended for v0.0.9 because v0.2 PureSignal will
need DDC2/DDC3 too.

### 3.3 Audio mix module (new file: `lyra/dsp/mix.py`)

This replaces aamix.c functionally.  Pure NumPy, ~80 LOC.  Contract:

```python
class StereoMixer:
    """Per-input pan + sum into a stereo (L,R) bus.

    Pan-curve ported from wdsp/patchpanel.c::SetRXAPanelPan (sin-pi rule;
    1.0 always on the dominant side, sin(pan*pi) on the other). Cite WDSP
    in the port-attribution docstring.
    """
    def __init__(self, n_inputs: int):
        self._pan = [0.5] * n_inputs        # default centered
        self._gainL = [1.0] * n_inputs
        self._gainR = [1.0] * n_inputs
        self._enabled = [False] * n_inputs

    def set_pan(self, idx: int, pan: float) -> None:
        # Compute L/R gain pair from pan; the dominant side is unity.
        ...

    def set_enabled(self, idx: int, on: bool) -> None: ...

    def mix(self, audio_per_input: list[np.ndarray | None]) -> np.ndarray:
        """Return shape (N, 2) float32 stereo. Inputs that are None or
        disabled contribute silence."""
        ...
```

**Threading note**: this runs on Lyra's DSP worker thread.  The audio
sink (`AK4951Sink.write(stereo)`) consumes the (N, 2) array.
Single-thread end-to-end on the worker — no locks needed inside
StereoMixer because pan-set comes from the Qt main thread but reads/
writes a single float (Python's GIL covers that for set/get).

**L/R swap**: a top-level boolean on StereoMixer that flips columns
just before output.  Mirrors `swap_audio_channels` in `network.h:110`,
`networkproto1.c:1231–1238`.

### 3.4 Channel-list refactor

Lyra's `Radio` (`lyra/radio.py`) is one big class with `_freq_hz`,
`_mode`, `_volume`, etc. as scalars.  The plan calls for a dict of
channels.

**Recommendation**: don't refactor in one big sweep.  Use the
**facade pattern**:

1. Add a small `_RxChannel` dataclass with `(freq_hz, mode, gain_db,
   notches, dsp_state)`.  Move the existing scalar fields into
   `_RxChannel(0)` only — leave Radio's `freq_hz` etc. as `@property`
   reading `self._channels[self._focused_idx].freq_hz`.
2. UI signals reroute through `set_freq_hz(self, hz, channel=0)` —
   default keeps single-channel callers working.
3. RX2 enable creates `self._channels[1] = _RxChannel(1, ...)` with
   its own DSP `Channel`.

Phase the refactor as the prior plan's "Phase 0" — ship as v0.0.7.1
if it stretches.

### 3.5 Threading and buffer flow

Today: HL2Stream's `_rx_loop` thread → `on_samples(samples, stats)`
→ Radio's worker (or main thread via signal, depending on v0.0.9
worker-mode setting) → DSP Channel → AudioSink → back into HL2Stream's
TX queue.

For RX2, the diagram becomes:

```
HL2Stream._rx_loop  → parser splits to {0,1,2,3}
                    → on_ddc_samples(ddc=0, ...) → Radio.dispatch_rx1
                    → on_ddc_samples(ddc=1, ...) → Radio.dispatch_rx2
                    → on_ddc_samples(ddc=2, ...) → Radio.dispatch_ps_feedback (v0.2; v0.0.9 = drop)

Radio.dispatch_rx*  → DspChannel[k].process(iq) → audio_k (np.ndarray)
                    → buffered until both channels have audio for this packet
                    → StereoMixer.mix([audio_0, audio_1]) → stereo
                    → audio_sink.write(stereo)
```

**Key contract**: dispatch_rx1 and dispatch_rx2 fire on the same
parser invocation, in sequence.  Both produce equal-length audio
(since both run at the same audio rate, even if their IQ rates
differ — DspChannel decimates internally).  The mixer can therefore
be called once per parser dispatch with both channels' audio in hand.
No queueing latency, no cross-thread fan-out.

If RX1 and RX2 run at **different IQ rates** (Thetis allows this —
see `cmaster.c::SetDDCRate`), DspChannel's internal decimator handles
it transparently because it's already designed to map any in_rate →
fixed audio_rate.  Lyra's existing `_Decimator` in `dsp/channel.py`
lines 49–71 already supports this.  **No new code needed for per-DDC
sample-rate independence.**

### 3.6 RX2 bench tests on N8SDR's HL2+

1. **Dual-tone test**: signal generator + dummy load injecting two
   distinct CW tones at separate freqs.  Tune RX1 to one, RX2 to the
   other (not within RX1's bandwidth).  Verify both audible
   simultaneously, hard-panned L/R.  Wireshark-confirm DDC0 and DDC1
   carry distinct samples by inspecting one EP6 frame: 19 26-byte
   slots, bytes 0–5 and 6–11 should show different I/Q magnitudes
   when both tones are present.

2. **DDC2/DDC3 silent-with-PS-off check**: at rest (no TX), capture
   EP6.  Bytes 12–17 (DDC2) and 18–23 (DDC3) should be near-zero —
   gateware open-input noise floor.  Confirms DDC2/DDC3 frame-write
   traffic causes no on-air or audio side effect.

3. **EP6 freq-drift confirmation**: tune RX1 to a known WWV freq.
   Confirm the right slot contains the carrier.  Move RX2 around —
   confirm RX1's slot doesn't shift, RX2's slot does.  Catches a
   bytewise interleave bug.

4. **Pan reproducibility against Thetis**: set pan=0.3 in Lyra, set
   pan=0.3 in Thetis (same RX, same signal).  Should sound identical
   because both use the WDSP sin-pi curve.

5. **L/R swap operator-test**: enable RX2, listen, then toggle "Swap
   L/R audio".  RX1 should move to right ear, RX2 to left.  Confirms
   the swap option works on the AK4951 path.

6. **Round-trip persistence**: RX2 freq, mode, pan, lock state — all
   survive Lyra restart with new QSettings keys.

---

## 4. TX implementation playbook (v0.1)

### 4.1 Strategic decision: Python TXA vs C extension

**Recommendation: Python-native TXA, minimum viable on day one.**

Rationale:

- Thetis's TXA chain is enormous — `wdsp/TXA.c` constructs ~25
  sub-modules (resamplers, panel, gen, phrot, eqp, preemph, leveler,
  cfcomp, compressor, bandpasses, alc, ammod, fmmod, osctrl,
  iqc.p0+p1, calcc, cfir, cfir, syncbuffs).  Most are unused in
  default SSB mode; the build-time wiring is `if (run==0) ... still
  create_*` so they exist but bypass.
- The **on-the-air-required** chain for SSB is: mic input → bandpass
  → SSB modulator (analytic-signal Hilbert + filter pair) → ALC →
  I/Q output.  That's ~5 modules out of 25.
- Python NumPy can sustain 48 kHz mono mic + 48 kHz I/Q out at ~ 400
  µs per 64-sample buffer with FFT-based bandpass.  Plenty of headroom.
- A C extension introduces wheel-build complexity (manylinux/Windows
  wheels) that's against Lyra's "pip install and go" ethos.

**Phased TX delivery**:

- **v0.1.0**: SSB only (USB/LSB).  Mic → bandpass → SSB modulator
  → ALC → EP2.  PTT state machine, drive-level slider, fwd/rev power
  meter.  No PS hooks, no compressor.  Clean linear PA operation
  only.
- **v0.1.1**: CW (key + sidetone, internal keyer; CWX PTT bit on
  HL2).  AM (carrier injection).  Compressor (port `wdsp/compress.c`
  — small file).
- **v0.1.2**: FM (deviation, preemph).  CFC (continuous frequency
  compressor — port `cfcomp.c`).
- **v0.1.3**: Leveler, equalizer.

### 4.2 Modules to write (Lyra-native)

`lyra/dsp/tx_channel.py` (new file, mirrors `dsp/channel.py` for RX):

```python
class TxChannel:
    """Mic input → modulator → I/Q output. SSB-only at v0.1.0.

    Contract:
      - Inputs: float32 mic samples at 48 kHz, arbitrary block size
      - Outputs: complex64 I/Q at 48 kHz (HL2 EP2 expectation)
      - Mode setter switches modulator (SSB-USB, SSB-LSB, CW, AM, FM)
      - alc_enable / alc_target: ALC stage setters
    """
    def __init__(self, mic_rate: int = 48000, iq_rate: int = 48000):
        ...

    def process(self, mic_block: np.ndarray) -> np.ndarray:
        """Returns shape (N,) complex64 I/Q at iq_rate."""
        ...
```

`lyra/dsp/ssb_mod.py` (new): analytic-signal SSB modulator using
scipy.signal.hilbert + bandpass.  ~50 LOC.  The math is well-known;
no porting attribution needed.

`lyra/dsp/cw_keyer.py` (new in v0.1.1): internal keyer state
machine matching `wdsp/main.c::keyer*` patterns — but trivial enough
to write Lyra-native.  dot/dash bytes go to the EP2 frame builder via
the protocol layer.

`lyra/radio/ptt.py` (new): PTT state machine.  Sources: UI MOX
button, future CAT, future hardware.  Modes: RX, MOX (TX-engaged),
TUN (tune at low power), CWX.  RX-mute behavior: when MOX→true, RX1
audio fades to zero over `tau_rx_mute` (default 50 ms — match Thetis
at the source).  When MOX→false, fade back.  The state machine emits
Qt signals so UI can disable RX-side controls during TX.

### 4.3 Protocol bits and bytes for TX

**EP2 frame extension** (already partially in `_pack_audio_bytes` for
AK4951 audio path):

- Each 8-byte slot: `[L_msb L_lsb] [R_msb R_lsb] [I_msb I_lsb] [Q_msb
  Q_lsb]`.  BE 16-bit signed.  Existing Lyra packer already does this
  — for TX, the I/Q slots get filled instead of zero.  Per
  `networkproto1.c::sendProtocol1Samples` lines 1241–1259, the
  formula is `int16 = round(sample * 32767)` with explicit `floor +
  0.5` / `ceil - 0.5` for round-to-nearest.
- 63 LRIQ tuples per USB frame, 2 USB frames per UDP datagram
  (already in Lyra).
- I/Q rate is fixed at **48 kHz** on HL2 — no resampling needed.
  Confirmed by inspection: the EP2 path is one set of LRIQ tuples per
  USB frame regardless of EP6 RX rate.  HL2 gateware clocks the DAC
  at 48 kHz.

**MOX bit (C0 bit 0)**: Lyra currently sets `frame[block_off + 3] =
c0 & 0xFE` to clear bit 0 (force RX).  For TX, set bit 0.  The C&C
round-robin's `c0` byte gets OR'd with `XmitBit & 1` — implement as:

```python
def _build_ep2_frame(self, c0, c1, c2, c3, c4, mox: bool = False):
    ...
    frame[block_off + 3] = (c0 | 0x01) if mox else (c0 & 0xFE)
```

**CW dot/dash bits**: per `networkproto1.c:1247–1258`, when in CW
mode, the I-sample LSB (the second byte of TX I-MSB or rather the
2-byte I word's low byte) gets overwritten with key state.
**HL2-specific** at lines 1249–1252: `(cwx_ptt << 3 | dot << 2 | dash
<< 1 | cwx) & 0x0F` packed into the I LSB.  Standard HPSDR has only
3 bits; HL2 uses bit 3 for CWX PTT.

**Frame 17 — TX latency / PTT hang** (`C0=0x2E`): per
`networkproto1.c:1162–1168`:

- C3 = `ptt_hang & 0x1F` (5 bits, range 0..31, sample units at 48 kHz
  → 0..650 ms)
- C4 = `tx_latency & 0x7F` (7 bits, range 0..127, ms — confirm via
  gateware docs; Thetis treats it as ms)

**Recommended HL2 defaults**: tx_latency=10ms, ptt_hang=4 (samples)
per pi-HPSDR community values.  Lyra exposes these in the TX setup
tab in v0.1.

**Drive level** — frame 10 C1 (`networkproto1.c:1078`):
`prn->tx[0].drive_level` is a 0..255 byte.  Operator-facing: a 0..100%
slider.

**TX attenuator** — frame 11 C4 when `XmitBit==1` (line 1100):
`tx_step_attn & 0x3F` plus override-enable bit 6 set → `0x40 | (att &
0x3F)`.  **HL2 quirk**: 6-bit signed range is interpreted as
-28..+31 dB rather than 0..31 like ANAN.  Lyra's UI label this as
"TX gain" (negative = attenuation, positive = gain) to match operator
mental model.  **This is PS-relevant** — auto-cal adjusts this — but
it's also the standard TX gain control on HL2.  v0.1 exposes it as
a slider.

**PA-on bit** — frame 10 C3 bit 7 (line 1084): `(pa & 1) << 7`.  HL2
with the on-board MITSUBISHI PA module wants this set.  Lyra default:
PA-on = True (operator can disable for low-power test work).

### 4.4 Forward / reverse power decoding

Already in Lyra for fwd/rev power **ADC values**, lacking watt-
conversion.  Per `networkproto1.c:506–514`:

- C0=0x08 (addr 1): C1:C2 = exciter_power AIN5 (drive monitor),
  C3:C4 = fwd_power AIN1 (PA coupler)
- C0=0x10 (addr 2): C1:C2 = rev_power AIN2, C3:C4 = AIN3 (HL2+ PA
  volts)

**Watt conversion** for HL2 fwd/rev (community values; Thetis at
`console.cs::computeAlexFwdPower` uses calibration constants per
board variant):

```python
# fwd_pwr_watts = (fwd_adc / 4096.0 * 3.3) ** 2 * coupler_factor
# coupler_factor depends on the PA tap; HL2 stock ≈ 1.5 (community number)
# Operator should be able to calibrate against a known dummy-load wattmeter.
```

Recommend Lyra exposes a "PA calibration" entry in Settings → TX with
a "set 25W = X" two-point calibration.  Then the formula reduces to a
learned scalar.

### 4.5 PTT state machine

From operator-side controls + radio feedback:

```
States:    RX (default) → MOX_TX (UI button or CAT) → RX
                       → CW_TX (key down sources keyer) → RX with hang-time
                       → TUN_TX (TUNE button drives at low power) → RX
                       → VOX_TX (audio level threshold; deferred to v0.2)
Inputs:    UI MOX button, UI TUN button, CAT command,
           keyer state (dot/dash detected), hardware PTT (deferred — HL2 bit feedback)
Outputs:   per-EP2-frame mox bit, ptt_hang_active flag,
           rx_mute_envelope (0..1 fade), drive_level scale,
           Qt signal mox_changed(bool) for UI
```

The state machine lives in `lyra/radio/ptt.py`.  Radio holds one
instance; UI calls `radio.ptt.request(MOX_ON)`.  The protocol layer
reads `radio.ptt.is_tx` per frame.

Hardware PTT input from HL2 EP6: per `networkproto1.c:496–498`,
`prn->ptt_in = ControlBytesIn[0] & 0x1` and dot/dash similarly.
Lyra's parser already gets these — surface them as `stats.ptt_in`.
PTT **input** vs **output**: operator-side hardware PTT pushes the
radio to TX (radio-to-host signal); MOX is host→radio.  Both feed the
state machine.

### 4.6 TX bench tests

1. **Dummy load + power meter**: TX into 50Ω dummy; verify Lyra's
   reported fwd power matches the meter within 10%.
2. **SSB modulator quality**: TX a 1 kHz mic tone; spectrum-analyze
   the RF output.  USB carrier suppression > 50 dB; opposite-sideband
   suppression > 50 dB.  If less, the SSB modulator's Hilbert-pair
   phase needs tuning.
3. **CW key-down**: dummy load, key down at low power.  Verify the
   full CW envelope (rise/fall shaping) on a scope.  Match Thetis CW
   envelope against a captured reference.
4. **MOX→RX-mute timing**: VFO on a strong signal, MOX-key briefly.
   RX1 audio should fade in <50 ms.  Without this fade the operator
   hears a click each MOX cycle.
5. **Wireshark EP2 capture during TX**: confirm `c0 & 0x01 == 1` in
   C&C while keyed, `0` while not.  Confirm I/Q slots non-zero while
   keyed.
6. **CWX PTT bit on HL2**: in CW mode with CWX engaged, capture EP2
   → I-sample LSB should show bit 3 set (HL2-specific; not on stock
   HPSDR).

---

## 5. PureSignal implementation playbook (v0.2)

### 5.1 The algorithm in plain language

Read `wdsp/calcc.c` lines 324–483 (`calc()` function) and
`wdsp/iqc.c` lines 122–203 (`xiqc()` function).  The high-level
model:

PA distortion is modeled as a **complex-valued nonlinearity that
depends on input envelope**.  For each TX I/Q sample with envelope
`r = |I + jQ|`, the PA distorts by some `g(r) · e^{jφ(r)}`.
PureSignal:

1. **Collects matched pairs** `(tx_iq, rx_iq)` during transmit.  The
   tx is the host-generated sample; the rx is the corresponding
   feedback sample (delayed and scaled, with magnitude tracking the
   PA's nonlinear response).
2. **Bins them by envelope magnitude** into `ints` (typically 16)
   bins of `spi` (typically 256) samples each (`calcc.c::pscc`
   LCOLLECT state, lines 701–761).  The bin-ID is `n = (int)(env *
   hw_scale * ints)` for linear-mapping mode, or a binary-search via
   `tmap` for the convex-mapping mode (lines 712–725).
3. **When all bins are full** (`full_ints == ints`, line 747), kicks
   the calc thread (`Sem_CalcCorr` semaphore) which:
   - Computes `env_TX[i]` and `env_RX[i]` for each collected sample
     (line 328–331).
   - Builds `cm[i]` (magnitude correction), `cc[i]` (cosine of phase
     correction), `cs[i]` (sine of phase correction), each as cubic-
     spline coefficients keyed on TX envelope (lines 432–441 via
     `xbuilder` from `lmath.c`).
   - The math: for the `i`-th collected pair, normalize by `norm =
     env_TX·env_RX` and compute `ym[i] = hw_scale·env_TX /
     (rx_scale·env_RX)`, `yc[i] = (Itx·Irx + Qtx·Qrx) / norm`,
     `ys[i] = (-Itx·Qrx + Qtx·Irx) / norm` (lines 375–381).  These
     are the inverse of the PA's gain-and-phase-as-a-function-of-
     envelope.
   - Validates with `scheck()`/`rxscheck()` — rejects if NaN,
     all-zero, out-of-range (>1.07 max gain, etc.).
4. **Ships coefficients to iqc** via `SetTXAiqcSwap` (line 500).  iqc
   applies them on every outgoing I/Q sample with a Hann-windowed
   crossfade between the old and new coefficient set (lines 167–179)
   to avoid clicks.
5. **iqc applies** (lines 122–203 in `iqc.c`): for each output sample
   with envelope `r`, look up `cm[k]`/`cc[k]`/`cs[k]` cubic
   interpolated → `PRE_I = cm·(I·cc - Q·cs)`, `PRE_Q = cm·(I·cs +
   Q·cc)`.  This is the **predistorted** I/Q that gets sent to the PA.

### 5.2 Lyra implementation strategy

**Port both `calcc.c` and `iqc.c` to Python.**  Modules:

- `lyra/dsp/ps_calcc.py` (~600 LOC Python target): the binning,
  calculation thread, state machine.  Uses a `threading.Semaphore`
  for the calc-thread trigger (mirror `Sem_CalcCorr`).  The
  cubic-spline builder (`lmath.c::xbuilder`) is a small subroutine
  — port it inline.
- `lyra/dsp/ps_iqc.py` (~150 LOC): the per-sample apply.  The hot
  path is a NumPy vectorize: for an N-sample I/Q block, compute
  `env`, look up bin index, evaluate cubic polynomial → output I/Q.
  Bench at 48k I/Q rate this is microseconds per buffer.
- `lyra/dsp/ps_state.py` (~100 LOC): the LRESET / LWAIT / LMOXDELAY
  / LSETUP / LCOLLECT / MOXCHECK / LCALC / LDELAY / LSTAYON /
  LTURNON state machine from `calcc.c` lines 525–537.  Pure Python.

**Attribution comment** on each ported module:

```python
"""Predistortion calibration — ported from WDSP wdsp/calcc.c (GPL v3)
by Warren Pratt NR0V. Lyra retains GPL v3+ licensing; this port keeps
the algorithm semantics intact while replacing C/Win32 primitives with
threading.Semaphore and NumPy. See LICENSE-WDSP.md."""
```

### 5.3 Operator UX — the `PSDialog`

`PSForm.cs` shape (lines 113–117 enum, 553–820 timer logic):

- **Top toggle**: "PureSignal On/Off".  When On, asserts
  `puresignal_run=True` (already plumbed) and switches PS state to
  LWAIT.
- **Calibrate buttons**: "Single Cal" (one-shot calibration on next
  TX), "Two-Tone" (host-generated two-tone test signal injected into
  TX chain — Lyra adds a `gen.py` two-tone source).
- **Auto Calibrate checkbox**: toggle automode (continuous adaptive
  vs single-shot).
- **Auto Attenuate checkbox**: enables HL2-specific attenuator
  feedback loop in `PSForm.cs::timer2code` HL2 branch.
- **Sliders**: "Peak threshold" (`SetPSHWPeak`, default 0.233 for HL2
  per `clsHardwareSpecific.cs`), "Power tolerance" (`SetPSPtol`,
  default 0.8), "MOX delay" (`SetPSMoxDelay`, time after PTT before
  cal starts, default ~0.1 s), "Loop delay" (between successive cal
  cycles, default 0.5 s).
- **Readouts**: feedback level (env_maxtx), attenuator setting
  (-28..+31), # cals attempted (`info[5]`), state (`info[15]`),
  running flag (`info[14]`).

**HL2-specific bounds**: TX attenuator slider range -28..+31 (not
0..31), recalibrate trigger `FeedbackLevel > 181 || (FeedbackLevel <=
128 && cur_att > -28)`.  These are auto-attenuator state-machine
constants — port from `PSForm.cs::NeedToRecalibrate_HL2` line 1142.

### 5.4 Threading

- **PS calc thread** (new): one persistent thread, semaphore-driven,
  runs `calc()` when triggered.  Runs in parallel with the DSP
  worker.  Posts coefficients back via a thread-safe coefficient
  swap (mirror `SetTXAiqcSwap` semantics).  Lock-free read in the
  iqc apply path: use double-buffered coefficient arrays + atomic
  index swap.
- **iqc apply path**: runs on the DSP worker, in-line with TXA chain.
  Adds < 100 µs per 48k buffer.
- **State machine ticks**: every TX I/Q buffer.  The `pscc()`
  function in `calcc.c` is called once per TX buffer with the
  buffer's tx and rx pairs — Lyra's equivalent: `ps_state.tick(
  tx_block, rx_block)` from inside `TxChannel.process()` after the
  modulator step.

### 5.5 Persistence

Per `calcc.c::PSSaveCorrection` lines 539–569: coefficients written
as plain-text floats to disk, one row of (`pm[0..3]`, `pc[0..3]`,
`ps[0..3]`) per int, `ints` rows per file.  Restored via
`PSRestoreCorrection`.

Lyra: reuse the format for compatibility (operators may want to
migrate from Thetis-saved corrections).  Store at `~/.config/lyra/
ps_corrections/{band}/{date}.txt`.  Auto-save on PS-off; auto-restore
on PS-on if the most recent file is < 24 h old.

### 5.6 PS bench tests

1. **PS off baseline**: TX a known SSB tone into a spectrum-analyzed
   dummy load.  Note 3rd-order IMD and 5th-order IMD products.  This
   is the un-corrected baseline.
2. **Single Cal**: enable PS, hit Single Cal during a brief key-down.
   Re-test the IMD spectrum.  **Expect IMD3/IMD5 to drop 10–20 dB.**
   If less, the cal didn't converge — check `info[15]==LSTAYON` and
   `info[14]==1`.
3. **Auto Calibrate stability**: leave Auto Cal on for 5 minutes
   during continuous tuning across bands.  Confirm no spurious cal
   failures (`info[5]` increment count should be reasonable, scOK
   should stay true).
4. **HL2-specific feedback level test**: at full PA drive,
   FeedbackLevel readout in PSDialog should be in 100–180 range.
   Out of that range → attenuator auto-steps.
5. **Coefficient persistence**: cal, save, restart Lyra, restore from
   saved file.  Re-key — IMD should still be improved without a fresh
   calibration cycle.
6. **PS-mod required check**: with the HL2 hardware mod absent,
   calibration should never converge (DDC2/DDC3 won't show TX
   feedback).  Lyra should detect a stuck-LCOLLECT and time out
   gracefully — don't lock up if there's no feedback signal.

---

## 6. WDSP port plan (concrete, in priority order)

| # | WDSP file | LOC | Lyra target | Effort |
|---|---|---|---|---|
| 1 | `patchpanel.c` (just `SetRXAPanelPan`) | 50 (relevant) | `lyra/dsp/mix.py` (curve) | 1 hour |
| 2 | (No port) — write `lyra/dsp/mix.py` Lyra-native | n/a | `lyra/dsp/mix.py` (mixer) | 2 hours |
| 3 | (TX, v0.1): SSB modulator | n/a (Lyra-native) | `lyra/dsp/ssb_mod.py` | 1 day |
| 4 | (TX, v0.1): `compress.c` | ~150 | `lyra/dsp/tx_compressor.py` | 1 day |
| 5 | (PS, v0.2): `lmath.c::xbuilder` (cubic-spline coef builder) | ~200 | `lyra/dsp/ps_xbuilder.py` (or numpy.polynomial) | 2 days |
| 6 | (PS, v0.2): `calcc.c` (full) | 1164 | `lyra/dsp/ps_calcc.py` | 2 weeks |
| 7 | (PS, v0.2): `iqc.c` (full) | 315 | `lyra/dsp/ps_iqc.py` | 4 days |
| 8 | (PS, v0.2): `delay.c` (used by calcc for tx/rx alignment) | ~80 | `lyra/dsp/delay_line.py` | 4 hours |

**Files we deliberately do not port**: TXA.c / RXA.c (channel
scaffolding, Lyra-native), channel.c (buffer mgmt, Lyra-native),
aamix.c (mixer — Lyra-native), analyzer.c (spectrum — Lyra has its
own GPU widget), main.c (Win32 thread mgmt — Lyra uses Python
threading).

**Files we already ported**: `nr.py`, `nr2.py`, `lms.py`, `anf.py`,
`nb.py`, `squelch.py`.  Continue per-feature decisions (e.g., port
`wdsp/cfcomp.c` for v0.1.2 if compressor needs it).

---

## 7. Threading and buffer-flow architecture

**Final Lyra architecture across v0.0.9, v0.1, v0.2**:

```
Thread 1: HL2Stream._rx_loop          (recvfrom loop)
  → parses EP6 → on_ddc_samples(idx, iq_block)
  → emits Qt signal samples_ready or directly invokes Radio (depending on worker mode)

Thread 2: DSP worker                   (Lyra existing DspWorker)
  → DspChannel[0].process(iq) → audio_0
  → DspChannel[1].process(iq) → audio_1
  → StereoMixer.mix([audio_0, audio_1]) → stereo
  → audio_sink.write(stereo)
  → (TX path, v0.1+) TxChannel.process(mic_block) → tx_iq
  → HL2Stream.queue_tx_iq(tx_iq)
  → (PS path, v0.2) PsCalcc.tick(tx_iq, rx_feedback_iq)

Thread 3 (NEW in v0.2): PS calc thread
  → semaphore wait
  → calc() ← compute new coefficients
  → coefficient atomic-swap into PsIqc

Thread 4: HL2Stream TX writer          (existing)
  → drain queue at EP2 cadence (48 kHz / 380 Hz frame rate)

Thread 5: Qt main thread
  → all UI; reads Radio state via signals/slots only.
  → PTT button click → Radio.ptt.request(MOX_ON) → state-machine update
```

**Thread priorities**: Lyra doesn't currently use MMCSS.
Recommendation: don't add it for v0.0.9.  Python's GIL is the binding
constraint, not OS-thread priority.  If audio drops appear post-RX2,
profile first; MMCSS via ctypes is a tactical addition, not a
strategic one.

**Buffer sizes**: Lyra uses HL2's 126-sample / 504-sample buffers
natively.  WDSP's `dsp_size=64` is a WDSP-internal choice; Lyra's
per-channel decimator + demod don't depend on a fixed buffer size.
Keep that flexibility.

**Sample rates**: Lyra currently defaults to one rate.  Per the prior
plan, **per-DDC rate independence** is supported by HL2 protocol
(`SetDDCRate(i, ...)`, `cmaster.c`) and by Lyra's existing decimator
design.  Recommended v0.0.9 default: both DDCs at 192 kHz.  Add a
per-DDC rate dropdown in v0.0.9.x if testers ask.

---

## 8. Bench-test plan (consolidated, against N8SDR's HL2+)

In execution order:

**Phase 0 — refactor smoke (single-RX)**

- All v0.0.7 functions intact: tune, mode change, gain, NR/NB/ANF/SQ
  behavior unchanged.
- `nddc=1` keepalive still works (regression).

**Phase 1 — RX2 protocol**

- Wireshark `nddc=4` confirmation: capture EP6, frame 0 C4 = 0x1C.
- DDC0 / DDC1 distinct-tone test (§3.6 #1).
- DDC2 / DDC3 silent-with-PS-off (§3.6 #2).
- Per-DDC freq-drift (§3.6 #3).

**Phase 2 — Audio routing**

- Stereo split L=RX1 / R=RX2 (§3.6 #4).
- L/R swap operator-test (§3.6 #5).
- Operator pan-curve match Thetis (§3.6 #4).

**Phase 3 — UI integration**

- Ctrl+1/Ctrl+2 focus toggle.
- Click-to-tune within RX2 panadapter half doesn't move RX1.
- A↔B operations correct.

**Phase 5 — TX (v0.1.0)**

- Dummy load fwd power calibration (§4.6 #1).
- SSB modulator carrier and opposite-sideband suppression (§4.6 #2).
- MOX → RX-mute fade timing (§4.6 #4).
- Wireshark EP2 MOX-bit confirmation (§4.6 #5).

**Phase 6 — PureSignal (v0.2)**

- Single Cal IMD reduction (§5.6 #2).
- Auto Cal stability (§5.6 #3).
- HL2 feedback level + auto-attenuator behavior (§5.6 #4).
- Coefficient persistence (§5.6 #5).
- Hardware-mod-absent failure mode (§5.6 #6).

---

## 9. Open questions (genuinely empirical)

1. **DDC2/DDC3 sample-rate during PS+TX on HL2**: Thetis sets
   `Rate[0]=Rate[1]=rx1_rate` for HL2 (per `console.cs:8474–8485`),
   but DDC2/DDC3's actual delivery rate is gateware-determined.  A
   pcap during PS+TX will tell us how many slot-counts of DDC2/DDC3
   samples per UDP packet versus DDC0/DDC1.  If same → HL2 gateware
   delivers PS feedback at user rate.  If different → sample-rate
   matching needed in `ps_calcc.py`.

2. **HL2 mic samples — value or zero when AK4951 is the audio path?**
   Lyra's RX path gets mic samples via the same EP6 stream
   (`networkproto1.c:570–576`).  With the AK4951 codec on, do these
   mic bytes carry actual mic input, or are they zeroed/unused?  This
   affects whether v0.1's mic-input source is EP6-mic or PC-host-
   mic-via-sounddevice.  **Test**: at idle, blow into the HL2 mic
   input, see if EP6 mic bytes show signal.

3. **HL2 PA-on bit power-up state**: stock HL2 default for `pa & 1`
   is gateware-dependent.  Test: bring up Lyra without setting
   frame-10 C3 explicitly — does the PA come up enabled?  Should we
   explicitly set `pa=1` for safety?

4. **HL2 PA fwd/rev power calibration constants**: known to vary by
   board revision.  Operator self-calibration is the right answer; a
   2-point cal stored in QSettings.

5. **Gateware variant on N8SDR's specific HL2+**: known to support
   PS+ddc=4.  Document the version string in the
   `hl2_puresignal_audio_research.md` for future reference.

6. **AK4951 48 kHz lock vs 96+ kHz EP2 cadence**: Lyra already
   empirically determined AK4951 is hard-locked at 48 kHz.  Confirm
   by Wireshark whether HL2 gateware drops EP2 frames over the
   48k-based cadence or buffers them — affects how aggressive the
   EP2 throttling needs to be.

---

## 10. Final file-path summary

**Lyra files to create or edit** (path under `Y:\Claude local\
SDRProject\lyra\`):

- `protocol/stream.py` — extend round-robin C&C registers, rewrite
  EP6 parser for nddc=4, add per-DDC freq writers (DDC1/DDC2/DDC3),
  add `puresignal_run` flag (inert in v0.0.9).
- `dsp/mix.py` (NEW, v0.0.9) — StereoMixer with WDSP-style sin-pi pan
  curve.
- `dsp/channel.py` — already has the abstract DspChannel; add a
  multi-instance lifecycle.
- `dsp/tx_channel.py` (NEW, v0.1).
- `dsp/ssb_mod.py` (NEW, v0.1).
- `dsp/cw_keyer.py` (NEW, v0.1.1).
- `dsp/tx_compressor.py` (NEW, v0.1.1).
- `dsp/ps_calcc.py` (NEW, v0.2).
- `dsp/ps_iqc.py` (NEW, v0.2).
- `dsp/ps_xbuilder.py` (NEW, v0.2).
- `dsp/delay_line.py` (NEW, v0.2, used by ps_calcc).
- `radio/ptt.py` (NEW, v0.1).
- `radio.py` — extend to dict of channels + facades; add PTT
  integration; add PS protocol coordination in v0.2.
- `ui/ps_dialog.py` (NEW, v0.2) — modeled on `PSForm.cs`.
- `ui/panels.py` — promote RX2 placeholder; add focus state, A↔B/
  Swap/Lock buttons, MOX/TUN buttons (v0.1), PS toggle (v0.2).
- `ui/spectrum.py` — split-vertical mode for dual panadapter.

**Thetis source paths cited** (under `D:/sdrprojects/OpenHPSDR-
Thetis-2.10.3.13/Project Files/Source/`):

- `ChannelMaster/networkproto1.c:422–586` (HL2 read loop), `:869–
  1201` (HL2 write loop), `:1204–1267` (EP2 audio packing).
- `Console/console.cs::UpdateDDCs` (~lines 8214–8577).
- `Console/PSForm.cs:113–117` (state enum), `:553–820` (timer logic),
  `:1014–1072` (DllImports), `:1142–1145` (HL2 recalibrate).

**WDSP files we will port** (port-attribution citations under
`D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/Project Files/Source/wdsp/`):

- `patchpanel.c::SetRXAPanelPan` lines 158–176 (RX2 pan curve, v0.0.9).
- `calcc.c` (full file, v0.2 PS).
- `iqc.c` (full file, v0.2 PS).
- `lmath.c::xbuilder` (subset, v0.2 PS — locate via grep when ready).
- `delay.c` (subset, v0.2 PS).

The path forward is concrete, the prior research only needs the small
corrections in §2, and the core engineering decisions (Python-native
everywhere except for `calcc/iqc` which are worth porting verbatim)
keep Lyra simple and license-clean.
