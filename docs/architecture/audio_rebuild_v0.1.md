# Audio architecture rebuild for v0.0.9.2 / v0.1

**Status:** Design doc, awaiting operator + co-developer sign-off.
**Authors:** Rick Langford (N8SDR), Brent Crier (N9BC), Claude.
**Drafted:** 2026-05-04, after senior-engineering audit of Lyra
audio path vs Thetis 2.10.3.13.
**Supersedes (when shipped):** the residual-clicks PARKED note in
`CLAUDE.md` §9.6, the v0.0.9.1 100 ms pre-fill workaround in
`lyra/dsp/audio_sink.py`.
**Companion doc:** `audio_pops_audit.md` (the v0.0.7.1 quiet-pass
audit — covers what we already shipped; this doc covers what we
still need to ship).

---

## Why this doc exists

After v0.0.9.1 shipped (TCI audio + click-reduction pass), both
Rick (AK4951 sink, all IQ rates 48–384 kHz) and Brent (PortAudio
sink, 48 kHz IQ) report:

- **Persistent clicks/pops** at random intervals on every Lyra
  release going back to ~v0.0.7.x.  Both sinks, both users, all
  rates.  Brent confirms: "those don't exist in Thetis on same
  hardware."
- **Loud volume spikes ("oh-shit moments")** — Rick only, AK4951
  only, all IQ rates.  Brent does NOT see these.

Four prior audio band-aids shipped in v0.0.7.1 → v0.0.9.1 (per-
sample AGC envelope tracker, decimator state preservation,
AK4951 sink-swap fade-out, 100 ms pre-fill) each fixed a real
issue but did not eliminate the residual.  This doc is the
result of a code-level diff between Lyra's and Thetis's audio
production paths, performed 2026-05-04, with the goal of
identifying the ARCHITECTURAL difference that band-aids cannot
reach.

The summary, in one sentence:

> **Thetis uses synchronous blocking handshakes between the DSP
> producer and the USB writer; Lyra uses an async ring-buffer
> with no backpressure.  That is the difference, and it explains
> every click, every pop, and (probably) every spike.**

The rest of this doc lays out the evidence and the fix plan.

---

## 1. The architectural mismatch — exact code citations

### 1.1 Thetis's TX audio path

Reference: `D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/Project
Files/Source/ChannelMaster/`.

#### 1.1.1 Packer thread, real-time priority

`networkproto1.c::sendProtocol1Samples` line 1206:

```c
HANDLE hTask = AvSetMmThreadCharacteristics(TEXT("Pro Audio"),
                                            &taskIndex);
if (hTask != 0) AvSetMmThreadPriority(hTask, 2);
```

The packer thread registers with Windows **MMCSS at "Pro Audio"
priority class**.  This pre-empts UI threads, GC threads, and
most other competing work.  This is the OS-level mechanism that
keeps Thetis audio steady regardless of UI activity.

#### 1.1.2 Atomic L/R + I/Q semaphore wait

```c
WaitForMultipleObjects(2, prn->hsendEventHandles, TRUE, INFINITE);
//                       ^^                       ^^^^
//                       both must signal     wait forever
// hsendEventHandles[0] = hsendLRSem  (released when RX audio ready)
// hsendEventHandles[1] = hsendIQSem  (released when TX audio ready)
```

The packer cannot run until **both** L/R and I/Q buffers are
filled.  Atomic frame integrity is structural, not best-effort.

#### 1.1.3 Producer blocks until USB write completes

`network.c::sendOutbound` (lines 1237–1341):

```c
memcpy(prn->outLRbufp, out, sizeof(complex) * 126);
ReleaseSemaphore(prn->hsendLRSem, 1, 0);
WaitForSingleObject(prn->hobbuffsRun[1], INFINITE);  // ← blocks here
```

After USB send (`networkproto1.c::WriteMainLoop` line 866):

```c
MetisWriteFrame(0x02, FPGAWriteBufp);
ReleaseSemaphore(prn->hobbuffsRun[0], 1, 0);
ReleaseSemaphore(prn->hobbuffsRun[1], 1, 0);
```

The producer is held in `WaitForSingleObject` until the previous
USB write finishes.  It cannot pile up ahead of the writer.

#### 1.1.4 TX I/Q explicitly zeroed during RX

```c
if (!XmitBit) memset(prn->outIQbufp, 0, sizeof(complex) * 126);
```

Defensive: every frame, when not transmitting, TX I/Q is
explicitly zeroed.  Not relying on init-state.

#### 1.1.5 Properties this design guarantees

- **Underrun is structurally impossible.**  The packer can only
  run when both producer semaphores have signaled.  Zero-pad
  recovery is not part of the design.
- **Overrun is structurally impossible.**  The producer cannot
  run until the writer has consumed the previous frame.
- **L/R + I/Q stay temporally aligned.**  Both semaphores must
  signal atomically.
- **Cadence is producer-driven.**  Whatever rate the DSP can
  sustain is the rate the USB writer fires at.  No independent
  consumer cadence to mismatch against.

This is the textbook lock-step producer/consumer pattern with
OS-level real-time priority on the consumer.  Thirty years of
Windows pro-audio engineering.

### 1.2 Lyra's TX audio path

Reference: `Y:/Claude local/SDRProject/lyra/`.

#### 1.2.1 Bounded async ring deque, no priority

`lyra/protocol/stream.py` line 271:

```python
self._tx_audio: deque = deque(maxlen=48000)  # 1 second at 48 kHz
self._tx_audio_lock = threading.Lock()
```

A `collections.deque` with `maxlen=48000`.  The `_rx_loop`
thread is a Python `threading.Thread(daemon=True)` with no
priority assertion.

#### 1.2.2 Producer never blocks

`lyra/protocol/stream.py::queue_tx_audio` line 363, called from
`AK4951Sink.write` and `SoundDeviceSink.write`:

```python
with self._tx_audio_lock:
    free_slots = self._tx_audio.maxlen - len(self._tx_audio)
    if len(pairs) > free_slots:
        self.tx_audio_overruns += len(pairs) - free_slots
    self._tx_audio.extend(pairs)
```

If the deque is full, `deque.extend` **silently drops the
oldest entries**.  The producer is never held.

#### 1.2.3 Consumer zero-pads on underrun

`lyra/protocol/stream.py::_pack_audio_bytes` line 327:

```python
if avail < n_samples:
    self.tx_audio_underruns += 1
    pulled.extend([(0.0, 0.0)] * (n_samples - avail))
```

When the deque has fewer samples than requested, zero-padded
silence is injected into the EP2 frame.  At the AK4951 codec
this is an audible step-discontinuity = click.

#### 1.2.4 DSP runs on the Qt main thread (default)

`lyra/radio.py` line 1104:

```python
self._dsp_threading_mode_at_startup: str = self.DSP_THREADING_SINGLE
```

Default mode is `DSP_THREADING_SINGLE` → audio production runs
**on the Qt main thread**, alongside paint events, mouse
events, UI signal/slot dispatch, and the spectrum widget.
Worker mode (`DSP_THREADING_WORKER`) exists but is opt-in BETA.

#### 1.2.5 Producer/consumer cadence mismatch

- **Producer:** `PythonRxChannel.process` (`lyra/dsp/channel.py`
  line 799) returns audio in 1024-sample blocks (= 21.3 ms at
  48 kHz).  Bursty: entire DSP chain runs in one call, then
  thread waits for next IQ batch.
- **Consumer:** EP2 packer wants 126 samples per frame, sent
  every ~2.6 ms (380 Hz cadence).  Steady.

On paper the rates balance (1024 produced / 21.3 ms ≈ 1023
consumed / 21.3 ms).  In practice, **any jitter on the producer
side drains the deque toward zero**.  A 22 ms gap (1 ms of
jitter) consumes 1066 samples vs 1024 produced = net –42.
After ~100 such jitter events, the 100 ms pre-fill (4800
samples) is exhausted.  Underrun.  Click.

#### 1.2.6 Properties this design guarantees

- **Underrun produces zero-padded silence injection** = audible
  click on every codec/sink.
- **Overrun produces silently-dropped-oldest** = audible
  sample-time discontinuity.
- **No thread priority** = any UI activity, GC pause, or
  scheduler hiccup that takes the Qt main thread offline drains
  the buffer toward an underrun.
- **Cadence is consumer-driven, decoupled from producer.**
  Mismatches are absorbed by the deque; jitter accumulates
  until something gives.

### 1.3 Why TCI audio doesn't click

The TCI audio tap (`lyra/radio.py` line 5682:
`self.audio_for_tci_emit.emit(audio)`) emits a Qt signal that
hops via `QueuedConnection` to the TCI server slot.  The slot
sends a WebSocket frame.  **No ring buffer in the path.**  The
consumer (TCI client over WebSocket) absorbs producer
burstiness via TCP buffering.

This is also why enabling TCI server "regularizes" the AK4951
clicks for Rick — the QueuedConnection emit on every audio
block adds enough Qt event-loop machinery to slightly smooth
the producer cadence.  Side effect, not a fix.

---

## 2. The bug split — two distinct issues

### 2.1 Bug #1 — Universal clicks/pops

**Affects:** Both users, both sinks, all IQ rates.
**Root cause:** Producer/consumer cadence mismatch documented
above (§1.2.5) with zero-pad-on-underrun recovery (§1.2.3).
**Mechanism:** Bursty 1024-sample producer fights steady
126-sample consumer; any timing jitter drains the buffer; zero
samples get injected; codec/PortAudio plays a step-
discontinuity; operator hears a click.
**SoundDeviceSink has the same bug** with a numpy ring instead
of a deque (lines 322–326 of `audio_sink.py`).  Both sinks fail
the same way.

### 2.2 Bug #2 — Loud volume spikes

**Affects:** Rick only, AK4951 only, all IQ rates.
**Likely root cause:** HL2 gateware behavior on EP2 cadence
underrun.  When Lyra's EP2 send pauses (Qt main thread blocked,
GIL pressure), the gateware sees no fresh audio.  Hypothesized
behavior: replays last EP2 buffer.  If that buffer contained a
peak, operator hears the peak repeated multiple times = loud
sustained burst.
**Why Brent doesn't see it:** PortAudio's documented underrun
behavior is fill-with-silence, never replay.  His sink path is
immune to gateware replay regardless of producer stalls.
**Status:** Hypothesis.  Needs Wireshark capture during a
deliberate UI stall to confirm what the gateware actually does.
The 30-second test of Rick switching to PortAudio sink also
disambiguates: if spikes vanish on PortAudio, hypothesis
confirmed (it's the AK4951/EP2/gateware path).  If spikes
persist on PortAudio, hypothesis falsified (something
Rick-environment-specific that's not the sink).
**Note:** Once Bug #1 is fixed (§3.3 backpressure eliminates
EP2 underruns entirely), Bug #2 likely resolves itself — but we
should still capture the gateware behavior for documentation.

---

## 3. The rebuild plan

Six commits, in order.  Each is independently testable.  Each
ships as a pre-release at the end so we can isolate regressions.

### 3.1 Commit 1 — Promote `DSP_THREADING_WORKER` to default

**Change:** `lyra/radio.py` line 1104, flip default from
`DSP_THREADING_SINGLE` to `DSP_THREADING_WORKER`.  Demote
single-thread mode to legacy/debug (operator can still opt back
in via Settings if a regression appears).

**Rationale:** The Qt main thread should never run audio DSP.
It has paint events, mouse handling, and signal/slot dispatch
competing for it.  Any UI activity (window resize, spectrum
redraw, mouse drag) stalls audio production.  Worker mode has
been opt-in BETA for months; it works.

**Risk:** Low.  Worker mode has flight-tested.  Operator can
toggle back if needed.

**Cost:** 1–2 hours plus testing.

**Pre-release:** v0.0.9.2-pre1.  Both Rick and Brent flight-test
for 24 hours.

### 3.2 Commit 2 — Match channel block size to 126 samples

**Change:** `lyra/dsp/channel.py` block_size from 1024 →
126 (or 252, 504 — any multiple of 126).

**Rationale:** Producer cadence must match consumer cadence to
eliminate burstiness.  126 samples per block at 48 kHz fires
every 2.6 ms — same cadence as the EP2 packer.  No bursts, no
idle gaps, no underrun pressure on the deque.

**Risk:** Medium.  Block size affects every DSP module's
internal buffering (NR1, NR2, ANF, LMS, NB, squelch, APF).
Each needs verification at the new block size.  FFT-based
stages (NR1, NR2) care about block size; per-sample stages
don't.

**Cost:** 2–3 days including module-by-module verification.

**Pre-release:** v0.0.9.2-pre2.

### 3.3 Commit 3 — Real backpressure on the deque + ring

**Change:** Replace `deque.extend()` and the SoundDeviceSink
ring's drop-oldest with a `threading.Condition`-based
backpressure.  Producer waits on `not full`; consumer signals
when it pops samples.

**Rationale:** Same pattern Thetis uses (semaphores) but
written in Python's threading primitives.  Eliminates overrun
entirely.  Combined with §3.2's cadence match, eliminates
underrun entirely too.

**Risk:** Low.  `threading.Condition` is well-understood.
Deadlock risk is bounded because both sides hold the same lock
briefly.

**Cost:** 1 day.

**Pre-release:** v0.0.9.2-pre3.

### 3.4 Commit 4 — MMCSS Pro Audio priority on RT threads (Windows)

**Change:** `ctypes` call to
`AvSetMmThreadCharacteristics(TEXT("Pro Audio"), ...)` on the
DSP worker thread and the `_rx_loop` thread, when running on
Windows.  Linux/macOS get equivalent `SCHED_FIFO` calls in a
later phase.

**Rationale:** This is the same Windows API call Thetis makes.
Immunizes the audio path from UI, GC, and most scheduler
jitter.  Makes the DSP worker thread effectively "above" the
Qt main thread in scheduling priority.

**Risk:** Low.  Well-documented Windows API.  Failure mode is
"call returns NULL handle" → fall back to default priority,
log a warning, continue.

**Cost:** Half a day.

**Pre-release:** v0.0.9.2-pre4.

### 3.5 Commit 5 — Wireshark + document HL2 gateware EP2 behavior

**Change:** Documentation only.  Wireshark capture of EP2
stream during a deliberate Qt-main-thread stall (e.g.
aggressive window resize while listening).  Annotate the
capture: does EP2 cadence stay steady, drop frames, or pause?
What audio comes out the codec during the stall?  Update
`hl2_puresignal_audio_research.md` and CLAUDE.md §10.

**Rationale:** Once §3.1–§3.4 land, EP2 stalls become
structurally impossible, so this matters less for fixing the
bug.  But it answers the longstanding open question (CLAUDE.md
§10 #5) about gateware behavior, and validates that Bug #2 is
actually resolved by the architectural fix.

**Risk:** Zero — read-only.

**Cost:** 2 hours.

**Pre-release:** rolled into v0.0.9.2-pre4 or pre5 as a doc
update.

### 3.6 Commit 6 — Fix gap-fade-on-every-block bug

**Change:** `lyra/radio.py` lines 5944–5952.  The 10 ms
fade-in is currently applied unconditionally on every audio
block, not just when `seq_errors` increments.  Re-indent so
the fade-in is inside the seq_errors-incremented branch.

**Rationale:** Separate bug introduced in v0.0.9.1.  Cosmetic
in impact (slight attenuation of the first 10 ms of every
block) but it's still wrong.

**Risk:** Trivial.

**Cost:** 30 minutes.

**Pre-release:** v0.0.9.2-pre5.

---

## 4. Expected outcomes after rebuild

| Symptom | Before | After |
|---|---|---|
| Universal clicks/pops | Random, ~3/sec under load | Gone (cadence match + backpressure makes them structurally impossible) |
| Rick's loud spikes | "Oh-shit moments" on AK4951 | Likely gone (no EP2 underrun → no gateware replay).  Falsifiable via §3.5 capture. |
| Audio latency | 100 ms pre-fill + 21 ms block = ~121 ms | ~5 ms deque high-water + 2.6 ms block = ~8 ms.  Closer to Thetis. |
| CPU load | Single thread doing UI + DSP | Worker thread on its own; Qt main thread freed for UI.  Net CPU similar; UI responsiveness improved. |
| Foundation for v0.1 RX2 | Marginal — doubling DSP load risks amplifying the bug | Solid — backpressure + cadence match handles 2× DSP load without changes |

---

## 5. What we are explicitly NOT doing

These are deliberate non-goals.  Discuss before changing.

- **No Numba / Cython / C extension.**  Pure Python at the
  right block size + threading model handles 192k IQ + 48k
  audio + dual-RX with budget to spare.  C extensions add
  wheel-build complexity that conflicts with Lyra's "pip
  install and go" ethos (CLAUDE.md §4.3).
- **No further pre-fill bumps.**  100 ms → 200 ms → 500 ms
  doesn't fix the architecture.  Pre-fill stays as a defensive
  startup cushion (§3.3 makes its size irrelevant once
  backpressure is in).
- **No RX2 work until this lands.**  v0.1 was originally next-up
  but doubling DSP load on a marginal architecture is a fast
  way to ship a regression.  v0.0.9.2 audio rebuild gates v0.1.
- **No more audio band-aids.**  Four shipped in v0.0.7.1 →
  v0.0.9.1; each was correct in its own scope but the
  underlying architecture was wrong.  This is the real fix.

---

## 6. Open questions for Brent

1. **Sink test:** When Rick switches to PortAudio sink, do the
   loud spikes vanish (=AK4951-specific) or persist (=Rick's
   environment)?  This data point gates §3.5's gateware
   investigation priority.
2. **Block size choice:** 126 / 252 / 504 — any preference?
   Smaller = lower latency + more loop overhead; larger = more
   FFT efficiency + more cadence pressure.  Initial pick: 126
   (matches consumer exactly).  Easy to revise.
3. **MMCSS scope:** Just the DSP worker, or also `_rx_loop`?
   Probably both — RX network reads must keep up with EP6 packet
   arrival or we underrun upstream of the audio path.
4. **Sounddevice sink ring buffer math:** Currently 200 ms ring
   capacity (`SoundDeviceSink._RING_SECONDS = 0.200`).  After
   §3.2 + §3.3 we can drop this aggressively (probably to 20 ms).
   Worth discussing the right post-rebuild value.
5. **Anything Brent has noticed in his flight-testing that
   doesn't fit the Bug #1 / Bug #2 split above?**  Reality
   check on the diagnosis.

---

## 7. Rollback plan (high-level — see §9 for full detail)

If any commit produces a regression that the next two commits
don't resolve, we revert that commit and re-evaluate.  Branches:

- `feature/threaded-dsp` = active development trunk (where we
  are today).
- `feature/audio-architecture-v2` = where commits 1–6 land.
- `main` = published releases.

Pre-releases each phase mean we always have a known-good build
to fall back to for testers who don't want to ride the dev
branch.

§9 below covers the full risk-management plan: recovery points,
per-commit safety nets, settings escape hatches, operator
downgrade procedure, failure-detection telemetry, decision
criteria, and data-preservation guarantees.

---

## 8. Acknowledgments

Thetis 2.10.3.13 source by the OpenHPSDR project — referenced
under §2 of CLAUDE.md (Thetis is studied as a design reference;
no code is copied).  WDSP by Warren Pratt (NR0V) — referenced
where relevant.  All code in this rebuild remains GPL v3+ and
Lyra-native.

---

*Last updated: 2026-05-04.  Update this doc when commits land
or the plan shifts.*

---

## 9. Risk management + fallback plan

This section is the answer to "what's our backup if the rebuild
breaks something?"  It covers recovery points (where we can
roll back TO), per-commit safety nets (how we prevent regressions
from compounding), operator escape hatches (how an operator
recovers without our help), and decision criteria (when do we
revert vs forward-fix).

### 9.1 Recovery points — what's known-good

**Source-tree recovery point:** annotated tag `v0.0.9.1` at
commit `4484326` on `main`.  This is the last shipped, validated
state.  Operator + Brent both confirmed v0.0.9.1 audio is no
worse than prior releases (clicks present but tolerable; spikes
on Rick's rig only).  Recovery is `git checkout v0.0.9.1` for
source or branching off it for a hotfix.

**Binary recovery point:** GitHub Release v0.0.9.1 at
<https://github.com/N8SDR1/Lyra-SDR/releases/tag/v0.0.9.1>
with `Lyra-Setup-0.0.9.1.exe` attached.  Any operator can
download this and reinstall.  Re-installation does NOT touch
QSettings or `~/.config/lyra/` (operator data preserved — see
§9.7).

**Branch state at start of rebuild:**

```
main                    = v0.0.9.1  (4484326)
feature/threaded-dsp    = dev trunk; ahead of main by:
                            b23b1bb  update-check 4-component fix
                            b95453c  audio_rebuild_v0.1.md
                            (this commit) §9 risk plan
feature/audio-architecture-v2  = TO BE CREATED off feature/threaded-dsp
                                  before Commit 1 starts
```

**Branch invariant during rebuild:** `main` does NOT move until
the rebuild lands fully and both testers sign off.  Operators
on `main` continue to see v0.0.9.1 as the latest release.

### 9.2 Per-commit safety net

Each of Commits 1–6 lands on its own short-lived intermediate
branch off `feature/audio-architecture-v2`, fast-forward merged
back when it passes flight-test.  This gives us:

- **Single-commit revertibility.**  `git revert <sha>` cleanly
  backs out one commit without disturbing the others.  No
  rebases, no history rewrites.
- **Bisectable regressions.**  If a regression shows up
  somewhere between Commit 2 and Commit 4, `git bisect` lands
  on the offender in two steps.
- **Per-commit pre-release on GitHub.**  Each Commit ships as
  `v0.0.9.2-preN` marked as pre-release.  Testers opt in.
  Public release feed shows v0.0.9.1 as the latest **stable**
  until the rebuild completes — non-tester operators don't see
  pre-releases unless they explicitly look.

**Branch hygiene during rebuild:**

```
feature/audio-architecture-v2          ← integration branch
  ├─ feature/audio-c1-worker-default   ← Commit 1 staging
  ├─ feature/audio-c2-block-size       ← Commit 2 staging
  ├─ feature/audio-c3-backpressure     ← Commit 3 staging
  ├─ feature/audio-c4-mmcss            ← Commit 4 staging
  ├─ feature/audio-c5-wireshark-doc    ← Commit 5 staging (doc only)
  └─ feature/audio-c6-gap-fade-fix     ← Commit 6 staging
```

A failed Commit-N branch can be deleted without affecting
Commits 1..N-1 already merged into the integration branch.

### 9.3 Operator escape hatches (settings-controlled, no rebuild needed)

Every architectural change must remain operator-toggleable from
the running app.  An operator hitting a regression should be
able to flip a switch and recover without a code change or
reinstall.  Concretely:

- **Commit 1 (worker default):** `DSP_THREADING_SINGLE` stays
  fully supported.  Settings → Advanced → "DSP threading mode"
  combo offers both choices.  Operator who hates worker mode
  flips back, restarts, problem gone.
- **Commit 2 (block size):** if 126 produces audible
  artifacts on some module, expose `audio_block_size` as a
  Settings → Advanced numeric field with allowed values 126 /
  252 / 504 / 1024.  Default 126; operator can revert per-rig.
- **Commit 3 (backpressure):** add a Settings → Advanced kill
  switch to fall back to drop-oldest behavior if the new
  backpressure ever deadlocks in the wild.  Default ON
  (backpressure active).
- **Commit 4 (MMCSS):** failure mode of the MMCSS API call is
  already "fall back to default priority + log warning."
  Already operator-safe.  Add Settings → Advanced "Disable
  MMCSS priority elevation" checkbox so operators can turn it
  off if a Windows version misbehaves.
- **Commit 5 (doc only):** no escape hatch needed — no code
  changes.
- **Commit 6 (gap-fade fix):** trivial; if it regresses for
  any reason, revert is a one-line edit.

**Defensive design rule:** every architectural change in this
rebuild ships with a Settings → Advanced toggle to disable it,
defaulting to ON (new behavior).  Once the rebuild has
flight-tested for a month, the dead toggles can be removed in
v0.1.x cleanup.

### 9.4 Operator downgrade procedure

If an operator on a v0.0.9.2-preN pre-release hits an issue
they can't work around with §9.3 toggles, the recovery is:

1. Open **Help → About** to confirm running version.
2. Visit <https://github.com/N8SDR1/Lyra-SDR/releases/tag/v0.0.9.1>.
3. Download `Lyra-Setup-0.0.9.1.exe`.
4. Run the installer (overwrites the v0.0.9.2-preN install).
5. Launch Lyra.  All settings preserved (see §9.7).

Document this procedure in the v0.0.9.2-preN release notes.
Brent can also help any tester directly via the issue tracker.

### 9.5 Failure detection telemetry

We need numbers, not vibes, to call a regression.  Telemetry
already in place + new additions for the rebuild phase:

**Already shipped (some hidden from UI in v0.0.9.1):**
- `tx_audio_underruns` counter on `HL2Stream` — increments
  every time the EP2 packer zero-pads silence into a frame.
- `tx_audio_overruns` counter on `HL2Stream` — increments
  every time `queue_tx_audio` saturates the 48 kHz deque.
- SoundDeviceSink ring `_overruns` / `_underruns` counters
  printed periodically to console (see `audio_sink.py` line
  383–409).
- `LYRA_AUDIO_DEBUG=1` env var — `Radio._diagnose_audio_step`
  prints rate-limited log lines for post-AGC sample-to-sample
  steps >0.05 amplitude (CLAUDE.md §9.6).

**New for rebuild phase (Commit 1 lands these):**
- **Re-expose** the underrun/overrun counters in the UI status
  bar during pre-release builds.  Hide them again in v0.0.9.2
  full release once they're confirmed steady at zero.
- **Worker-thread heartbeat** — count of blocks processed by
  the DSP worker thread, sampled at 1 Hz.  If it stops
  incrementing while audio is playing, the worker has stalled.
  Surfaced as a "DSP worker: N Hz" readout in the same area.
- **Deque high-water mark** — running max of the deque depth
  observed during the last 10 seconds.  Tells us if backpressure
  is actually engaging.

These give us objective regression detection — if Commit N
ships and the underrun counter stays at zero across an hour of
listening on both rigs, that's evidence the architectural fix
holds.  If the counter ticks up, we have a number to show.

### 9.6 Rollback decision criteria

Numbered tripwires.  If any of these hits, halt and re-evaluate
before continuing:

1. **Hard tripwire — any rebuild commit makes audio audibly
   worse than v0.0.9.1.**  Immediate revert.  Re-investigate
   before next attempt.  This is the line we do NOT cross.
2. **Soft tripwire — underrun counter > 1 per minute on either
   rig at the end of a rebuild commit's flight-test.**  Don't
   merge.  Diagnose before next commit.
3. **Soft tripwire — DSP worker heartbeat drops below 30 Hz
   sustained.**  Indicates worker is stalling.  Diagnose before
   next commit.
4. **Halt-rebuild tripwire — three consecutive commits fail
   their flight-test.**  Stop the rebuild.  The diagnosis is
   probably wrong; re-do the audit before resuming.
5. **Hard halt — any rebuild commit produces a deadlock,
   crash, or data corruption.**  Immediate revert + post-mortem
   doc before resuming.

**Forward-fix vs revert calls:**
- A new bug introduced by Commit N that's clearly bounded and
  fixable in Commit N+1 → forward-fix.
- A new bug whose scope is unclear or whose fix is non-trivial
  → revert Commit N, treat as a re-design.

### 9.7 Data preservation guarantees

Operator data MUST survive any rebuild commit, downgrade, or
upgrade in this cycle.  No schema bumps in the rebuild.

**Persistent data locations (Windows):**

```
%APPDATA%\N8SDR1\Lyra.ini                      ← QSettings
%LOCALAPPDATA%\Programs\Lyra\                  ← installed binaries
%USERPROFILE%\.config\lyra\noise_profiles\     ← captured noise profiles
%USERPROFILE%\.config\lyra\memory_bank.csv     ← memory bank entries
%USERPROFILE%\.config\lyra\ps_corrections\     ← PS coefficients (v0.3+)
```

**Guarantees the rebuild MUST preserve:**
- QSettings keys: no renames, no removals, no type changes.
  New keys may be added; old keys remain readable.  Specifically
  the existing `update_check/*`, `tci/*`, `audio/*`, `dsp/*`
  trees stay schema-stable.
- Noise profile files: format unchanged.  Operator-curated;
  loss = real loss.
- Memory bank CSV: format unchanged.
- Captured profiles, memory entries, station data: all
  untouched by the rebuild.

If any rebuild commit needs a new QSettings key, it gets a
default value that produces v0.0.9.1-equivalent behavior, and
the schema migration (if any) is one-way only — old install
reads new keys with defaults.

### 9.8 Communication plan to testers

For each pre-release, the GitHub release notes must include:

1. **What changed** — concrete list of behavioral changes.
2. **What to test** — specific things we want eyes on.
3. **Known issues** — anything we already know is regressed.
4. **Recovery procedure** — link to §9.4 if anything goes
   wrong.
5. **Tracker link** — where to file an issue.

Pre-release tag format: `v0.0.9.2-preN` where N = commit number.
Marked as **pre-release** on GitHub so the auto-update silent
checker (post-fix) flags it for opted-in testers but not
random downloaders.

### 9.9 Hard non-negotiables

These are commitments we make BEFORE the rebuild starts and
hold through to ship:

1. `main` does NOT move from v0.0.9.1 until the full rebuild
   passes flight-test from BOTH operators.
2. The v0.0.9.1 GitHub Release is NEVER deleted, edited, or
   marked pre-release.  It is the permanent fallback binary.
3. No operator data is lost in any commit, ever.  Schema bumps
   are forbidden in this rebuild scope.
4. Every architectural change ships with an operator-toggleable
   off-switch via Settings → Advanced.
5. If the diagnosis is proven wrong by Commit 1 data, we STOP
   and re-investigate before continuing.  Sunk cost is not a
   reason to continue.

### 9.10 What we'll know at each milestone

| After this commit | We will know |
|---|---|
| Commit 1 lands + 1 week test | Whether moving DSP off Qt main thread alone reduces clicks measurably.  Tells us if cadence-mismatch hypothesis is right. |
| Commit 2 lands + 1 week test | Whether matching producer/consumer block sizes eliminates underruns at zero load.  Tells us if cadence is the only producer-side issue. |
| Commit 3 lands + 1 week test | Whether real backpressure handles UI-load and stress conditions.  Tells us the design is robust. |
| Commit 4 lands + 1 week test | Whether MMCSS priority closes the last jitter source on Windows.  Tells us about scheduler interactions. |
| Commit 5 (Wireshark doc) | Whether HL2 gateware replays last buffer on EP2 underrun.  Resolves Bug #2 hypothesis empirically. |
| Commit 6 (gap-fade fix) | One-line cleanup; mostly cosmetic. |
| All 6 + 2 weeks total test | Whether v0.0.9.2 is ship-ready or needs another iteration. |

If we get to "all 6 + 2 weeks" and the audio is still problematic,
we revert `feature/audio-architecture-v2` entirely, return to
v0.0.9.1 as `main`, and re-do the audit from scratch with the
data we collected.  That's a worst-case outcome of three weeks
of work and is bounded — not an open-ended risk.
