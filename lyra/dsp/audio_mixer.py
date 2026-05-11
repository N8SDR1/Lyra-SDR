"""Lyra audio mixer + dispatcher thread.

Port of Thetis ChannelMaster\\aamix.c (Warren Pratt / NR0V WDSP-style
glue, GPL v3+; Lyra is GPL v3+, license-compatible).  See
``docs/architecture/wdsp_integration.md`` for the attribution policy.
The architecture decision lives in CLAUDE.md §13.

Why this module exists
======================

Thetis's audio output path inserts a dedicated **audio mixer thread**
between the DSP channels and the network senders.  Producers (DSP
RX channels) push audio into per-stream input rings via
``add_audio_input`` (non-blocking) and signal a per-stream Ready
semaphore once per ``outsize`` (= 126) samples produced.  The mixer
thread waits for ALL active streams to be ready, mixes ``outsize``
samples per stream into a single L/R output buffer, and dispatches
that output to a registered ``Outbound`` callback.

The Outbound callback is the **lockstep gate**.  For Thetis's HL2
audio path it copies the buffer to ``outLRbufp``, releases
``hsendLRSem``, then **blocks** on ``WaitForSingleObject(hobbuffsRun
[1], INFINITE)``.  ``WriteMainLoop_HL2`` (the EP2 writer) drains
``outLRbufp`` into a UDP packet, sends, and releases
``hobbuffsRun[1]`` — unblocking the mixer.  Net effect: the mixer
runs at exactly the wire cadence (380.95 Hz at HL2's 48 kHz codec
rate), regardless of how bursty the producer is.

Lyra previously lacked this thread.  The DSP worker thread did
both signal processing AND audio queueing inline; when a 512-sample
DSP block produced 4 frames worth of audio, the EP2 writer drained
all 4 in <1 ms then sat idle for ~10 ms — wire cadence was bursty,
which on the operator's HL2+ produced audible clicks ("we are still
missing something Thetis is doing" — operator feedback 2026-05-06,
v0.0.9.6 round 12).

This module ports the missing thread.  Producer (DSP worker)
remains non-blocking; mixer thread paces the wire.

Lyra-specific simplifications for v0.0.9.6
==========================================

For the v0.0.9.6 audio-foundation release Lyra has only one active
RX stream.  The mixer is built to support multiple streams (the
n_inputs constructor argument) so v0.1 RX2 work just adds a second
stream and per-stream pan/volume.  For now the mixer passes through
one input directly to outbound, with no inter-stream mixing logic.

Lyra also doesn't yet implement aamix's slewing (fade-in/out on
stream activation).  v0.1 stereo split and v0.2 TX path will need
slewing for click-free RX2 toggle and PTT transitions; that lands
when those features land.

Reference (line numbers in the Thetis 2.10.3.13 tree):
  - ``ChannelMaster/aamix.c::mix_main``        line 32-49
  - ``ChannelMaster/aamix.c::add_audio_input`` lines 235-278
  - ``ChannelMaster/aamix.c::xaamix``          lines 423-459
  - ``ChannelMaster/network.c::WriteUDPFrame`` lines 1287-1339
    (the lockstep outbound for HL2 HERMES path)
  - ``ChannelMaster/netInterface.c``           lines 1749-1761
    (audioCodecId-driven outbound dispatch)
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import numpy as np

_log = logging.getLogger(__name__)


# Default frame size = 126 samples per outbound dispatch.  This
# matches HL2's EP2 frame slot size exactly (63 LRIQ tuples per USB
# block × 2 USB blocks per UDP datagram = 126 LR pairs per packet).
# At 48 kHz codec rate that's 2.625 ms per outbound call = 380.952 Hz
# wire cadence.  Don't change without also auditing every consumer
# (HL2 EP2 writer draws exactly 126; SoundDeviceSink ring expects
# this granularity for its RMatch input cadence).
DEFAULT_OUTSIZE = 126

# Default per-stream input ring size = 24 × outsize = 3024 samples =
# 63 ms at 48 kHz.  Generous headroom for producer-side bursts: a
# Lyra DSP block of 512 samples (10.7 ms at 48 kHz audio output)
# easily fits with margin for jitter from GIL / GC pauses.  Thetis's
# ``aamix.c`` uses 24 × outsize as well (see ``rsize`` parameter
# passed to ``create_aamix`` in ``cmaster.c``).
DEFAULT_RING_SIZE = 24 * DEFAULT_OUTSIZE


# ────────────────────────────────────────────────────────────────────
# Destination-mask bit positions (Phase 0 scaffolding for the 8-way
# AAmixer state machine per consensus-plan §3.3 IM-3).
#
# Thetis flattens (rx_index, sub_index) into a single mixinid via
# WDSP.id(rx, sub) (cmaster.c:533, 552) and treats each stream as a
# bit position in a 32-bit mask for SetAAudioMixStates(valid_mask,
# active_mask).  Lyra uses Lyra host channel IDs as bit positions.
#
# Phase 0 only honors DEST_L | DEST_R for host_ch=0 (= the live
# RX1-only route).  Phase 1 RX2 adds DEST_L for RX1 + DEST_R for RX2;
# v0.2 TX-mon and v0.3 PS-disable-RX flip these via the state-product
# transitions in set_state().
DEST_L = 0x01    # left output channel
DEST_R = 0x02    # right output channel
DEST_LR = DEST_L | DEST_R    # both (RX1-only mono-on-stereo today)


# ────────────────────────────────────────────────────────────────────
# Outbound callback type
# ────────────────────────────────────────────────────────────────────
#
# Outbound is called by the mixer thread once per outsize-sample
# output frame.  Signature:
#
#   outbound(samples: np.ndarray) -> None
#
# where samples is a (outsize, 2) float32 array of (L, R) pairs.
#
# For HL2 audio jack mode, outbound BLOCKS until the EP2 writer has
# sent the corresponding packet.  This is the lockstep gate.
#
# For PC Sound mode, outbound writes to the SoundDeviceSink's
# RMatch input + returns immediately; PortAudio drains the ring
# asynchronously.
#
# For NullOutbound (no sink attached), outbound returns immediately
# without doing anything.  The mixer thread keeps running but its
# output is discarded.

OutboundCallback = Callable[[np.ndarray], None]


def null_outbound(samples: np.ndarray) -> None:  # noqa: ARG001
    """Outbound that drops samples on the floor.  Safe default when
    no sink is attached.
    """
    return None


# ────────────────────────────────────────────────────────────────────
# AudioMixer
# ────────────────────────────────────────────────────────────────────


class AudioMixer:
    """Dedicated audio output dispatcher thread.

    Single producer-thread (or multiple, one per stream) pushes via
    ``add_input(stream_id, samples)``.  Mixer thread pulls
    ``outsize`` samples per active stream, dispatches via outbound.

    For v0.0.9.6 with one RX stream, this is effectively a
    non-blocking producer to lockstep-paced consumer bridge.  The
    threading shape is what matters; the mixing math (pan, balance,
    multi-stream sum) lands as part of v0.1 RX2 + v0.2 TX work,
    plumbed into ``xaamix_local`` below.
    """

    def __init__(
        self,
        n_inputs: int = 1,
        outsize: int = DEFAULT_OUTSIZE,
        ring_size: int = DEFAULT_RING_SIZE,
        thread_name: str = "lyra-audio-mixer",
    ) -> None:
        if n_inputs < 1:
            raise ValueError("n_inputs must be >= 1")
        if outsize <= 0:
            raise ValueError("outsize must be positive")
        if ring_size < 2 * outsize:
            raise ValueError(
                "ring_size must be at least 2 * outsize for safe wrap")
        self._n_inputs = n_inputs
        self._outsize = outsize
        self._ring_size = ring_size
        self._thread_name = thread_name

        # Per-stream state.  All ring access is guarded by
        # ``self._lock`` -- producers (DSP worker) and consumer
        # (mixer thread) both touch ``inidx`` / ``outidx`` /
        # ``unqueuedsamps`` so a single lock keeps it simple.
        self._ring = [
            np.zeros((ring_size, 2), dtype=np.float32)
            for _ in range(n_inputs)
        ]
        self._inidx = [0] * n_inputs
        self._outidx = [0] * n_inputs
        self._unqueuedsamps = [0] * n_inputs
        # ``ready[i]`` is incremented by 1 each time stream i has
        # produced ``outsize`` more samples.  The mixer thread
        # acquires ``ready[i]`` once per output frame (one per
        # active stream) before mixing.  Currently we only use
        # stream 0; v0.1 RX2 will extend to wait on multiple.
        self._ready = [
            threading.Semaphore(value=0)
            for _ in range(n_inputs)
        ]
        self._lock = threading.Lock()

        # Outbound callback + its lock (callback can be swapped at
        # runtime from any thread, e.g., when the operator changes
        # audio sinks in Settings).
        self._outbound: OutboundCallback = null_outbound
        self._outbound_lock = threading.Lock()

        # Active mask -- which streams the mixer should pull from
        # this iteration.  v0.0.9.6: stream 0 always active.  v0.1
        # extends so the mixer can run with RX2 on/off without a
        # full restart.
        self._active = [True] + [False] * (n_inputs - 1)

        # Per-stream destination mask (Phase 0 scaffolding per
        # consensus-plan §3.1.x item 2 + §3.3 IM-3).  Default route
        # for stream 0 is DEST_LR (= RX1 mono-on-stereo on both
        # channels) which preserves v0.0.9.x audible behavior.  All
        # other streams start with mask 0 (silent).  Phase 1 RX2
        # rewires stream 0 → DEST_L, stream 1 → DEST_R when the
        # operator enables RX2; Phase 0 stores values but only the
        # current live route is honored by _mixer_loop.
        self._route_mask = [DEST_LR] + [0] * (n_inputs - 1)

        # AAmixer state product (Phase 0 scaffolding per §3.3 IM-3).
        # Eight-way state machine: Power × MOX × diversity × PS,
        # most cases collapsing on no-power-no-MOX-no-PS.  Phase 0
        # accepts the state setters and stores values; no live
        # behavior change (the mixer still passes host_ch=0 through
        # to L+R unconditionally).  Phase 1 onward consume this
        # state to drive route remapping + slewing.  `tx_mon_active`
        # is in the signature upfront (B-3 fix) so v0.2 doesn't need
        # to change the API surface.
        self._state_power: bool = False
        self._state_mox: bool = False
        self._state_diversity: bool = False
        self._state_ps_enabled: bool = False
        self._state_rx2_enabled: bool = False
        self._state_tx_mon_active: bool = False

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Diagnostic counters (per second, surfaced if needed).
        self.frames_dispatched: int = 0
        self.input_overruns: int = 0   # producer outpaced consumer
        self.outbound_drops: int = 0   # outbound was None at dispatch

    # ── Producer interface ───────────────────────────────────────

    def add_input(self, stream_id: int, samples: np.ndarray) -> None:
        """Push samples to ``stream_id``'s input ring.

        ``samples`` must be a (N, 2) float32 (or castable) array of
        (L, R) pairs.  Non-blocking; intended to be called from the
        DSP worker thread.

        Mirrors Thetis ``add_audio_input`` (aamix.c:235-278) without
        the optional input-side resampling (Lyra's DSP chain
        produces at the codec rate already; resampling lives in
        SoundDeviceSink for the PC sound output path, not here).
        """
        if stream_id < 0 or stream_id >= self._n_inputs:
            raise ValueError(
                f"stream_id {stream_id} out of range "
                f"[0, {self._n_inputs})")
        if samples.size == 0:
            return
        a = np.asarray(samples, dtype=np.float32)
        if a.ndim == 1:
            # Mono -> duplicate to L/R.  Maintains symmetry with
            # the legacy AK4951Sink behaviour.
            a = np.stack((a, a), axis=1)
        elif a.ndim != 2 or a.shape[1] != 2:
            # Defensive: flatten and duplicate.  Don't drop audio
            # silently on an unexpected shape.
            flat = a.reshape(-1)
            a = np.stack((flat, flat), axis=1)
        n = a.shape[0]

        with self._lock:
            ring = self._ring[stream_id]
            inidx = self._inidx[stream_id]
            # Detect ring overrun.  At 24×outsize ring with a 4×outsize
            # producer block this should be impossible barring a
            # mixer-thread stall measured in tens of milliseconds.
            free = self._ring_size - self._unqueuedsamps[stream_id] - (
                inidx - self._outidx[stream_id]) % self._ring_size
            # ``free`` is roughly "ring slots not yet pending mix".
            # If the producer would write more than that, bump the
            # overrun counter so a regression is visible.  We still
            # write -- the wraparound will just clobber stale data
            # that hadn't been consumed yet, mirroring what Thetis
            # does (ring buffer; consumer falls behind => oldest
            # data lost).
            if n > free:
                self.input_overruns += (n - free)
            # Copy into the ring with wrap-around.
            first = min(n, self._ring_size - inidx)
            ring[inidx:inidx + first] = a[:first]
            if n > first:
                ring[:n - first] = a[first:]
            self._inidx[stream_id] = (inidx + n) % self._ring_size

            # Signal the Ready semaphore once per outsize samples
            # produced.  Carry the < outsize remainder forward so
            # bursty producers (DSP block of 512 = 4 × 126 + 8
            # leftover) signal the right number of times overall.
            self._unqueuedsamps[stream_id] += n
            if self._unqueuedsamps[stream_id] >= self._outsize:
                signals = self._unqueuedsamps[stream_id] // self._outsize
                self._unqueuedsamps[stream_id] -= signals * self._outsize
                # Release the semaphore N times (one per Ready chunk).
                for _ in range(signals):
                    self._ready[stream_id].release()

    def set_stream_active(self, stream_id: int, active: bool) -> None:
        """Enable or disable a stream in the mixer.  Disabled streams
        are skipped during mixing (their ring is ignored).  Used by
        v0.1 RX2 toggle (RX2 off -> pure RX1; RX2 on -> stereo split).
        """
        if stream_id < 0 or stream_id >= self._n_inputs:
            return
        with self._lock:
            self._active[stream_id] = active

    # ── Route + AAmixer state surface (Phase 0 scaffolding) ─────
    #
    # Phase 0 contract per consensus-plan §3.1.x item 2:
    #   * set_route(stream_id, dest_mask) and set_state(...) MUST
    #     exist with their final Phase 1+ signatures.
    #   * Only route `host_ch=0 → L+R sink` is wired live; all other
    #     routes are stored but inert (the mixer loop still passes
    #     host_ch=0 straight through to outbound).
    #   * `tx_mon_active` is in set_state's signature up-front (the
    #     B-3 fix from Round 1) so v0.2 TX-mon doesn't change the
    #     public API.
    #
    # Phase 1 RX2 plumbs set_route into _mixer_loop's mixing math
    # (DEST_L for RX1, DEST_R for RX2 when rx2_enabled=True).
    # v0.2/v0.3 use set_state to drive the 8-way state machine
    # (Power × MOX × diversity × PS) per §3.3 IM-3.

    def set_route(self, stream_id: int, dest_mask: int) -> None:
        """Set per-stream destination mask in the output buffer.

        ``dest_mask`` is a bitwise-OR of ``DEST_L`` and ``DEST_R``;
        ``0`` means "stream is silenced at the mixer output."  The
        bit-position pattern matches Thetis's mixinid scheme
        (aamix.c) so the same code structure absorbs RX2 (Phase 1),
        TX-mon (v0.2), and PS-disable-RX (v0.3) without an API
        rewrite.

        Phase 0 stores the value but the mixer loop currently
        ignores it -- the only live route is host_ch=0 → L+R,
        preserved by initialization defaults in ``__init__``.  Phase
        1 RX2 makes the mixer honor the mask.

        Safe to call from any thread.
        """
        if stream_id < 0 or stream_id >= self._n_inputs:
            raise ValueError(
                f"stream_id {stream_id} out of range "
                f"[0, {self._n_inputs})")
        if dest_mask & ~DEST_LR:
            raise ValueError(
                f"dest_mask 0x{dest_mask:x} has bits outside "
                f"DEST_L|DEST_R (0x{DEST_LR:x})")
        with self._lock:
            self._route_mask[stream_id] = dest_mask

    def set_state(
        self,
        power: bool,
        mox: bool,
        diversity: bool,
        ps_enabled: bool,
        rx2_enabled: bool,
        tx_mon_active: bool = False,
    ) -> None:
        """Update the AAmixer state product.

        Eight-way state machine (Power × MOX × diversity × PS, most
        cases collapsing on no-power-no-MOX-no-PS) per consensus-plan
        §3.3 IM-3.  ``tx_mon_active`` is the v0.2 TX-monitor axis,
        included in the Phase 0 signature so the public API stays
        stable across v0.1 → v0.2 → v0.3.

        Phase 0: values stored; no live behavior change (the mixer
        passes host_ch=0 through to L+R unconditionally regardless
        of state).  Phase 1+ consume this state to drive route
        remapping (RX2 enable), per-stream-mute multipliers
        (MuteRX1OnVFOBTX / MuteRX2OnVFOATX), and slewing on
        activation transitions.

        Safe to call from any thread.
        """
        with self._lock:
            self._state_power = bool(power)
            self._state_mox = bool(mox)
            self._state_diversity = bool(diversity)
            self._state_ps_enabled = bool(ps_enabled)
            self._state_rx2_enabled = bool(rx2_enabled)
            self._state_tx_mon_active = bool(tx_mon_active)

    # ── Outbound registration ────────────────────────────────────

    def set_outbound(self, callback: Optional[OutboundCallback]) -> None:
        """Register the outbound callback (or pass None to detach).

        Outbound is called by the mixer thread once per
        ``outsize``-sample output chunk with a (outsize, 2) float32
        array.  For HL2 mode the callback BLOCKS until the EP2
        writer has sent the packet (lockstep gate).  For PC sound
        mode the callback writes to the sink's ring and returns.

        Safe to call from any thread.  The current dispatch in flight
        (if any) is allowed to complete; subsequent dispatches use
        the new callback.
        """
        cb = callback if callback is not None else null_outbound
        with self._outbound_lock:
            self._outbound = cb

    # ── Thread lifecycle ─────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._mixer_loop,
            name=self._thread_name,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        # Wake the mixer thread by releasing each Ready semaphore --
        # otherwise it could be blocked indefinitely on .acquire()
        # if the producer happened to be quiet at stop time.
        for sem in self._ready:
            sem.release()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ── Mixer thread ─────────────────────────────────────────────

    def _mixer_loop(self) -> None:
        """The mixer thread.  Mirrors Thetis ``aamix.c::mix_main``.

        Loop:
          1. Wait for all ACTIVE streams' Ready semaphores.
          2. Pull ``outsize`` samples from each active stream's
             ring into a per-stream view.
          3. Mix into the output buffer (currently passthrough
             from stream 0; v0.1 RX2 + v0.2 TX add real mixing).
          4. Dispatch via outbound (which blocks for HL2 lockstep).

        On Windows we elevate to MMCSS Pro Audio priority,
        matching Thetis's ``mix_main`` thread setup
        (``AvSetMmThreadCharacteristics(TEXT("Pro Audio"))``).
        """
        # NOTE on thread priority:  Thetis's mix_main runs at MMCSS
        # Pro Audio (AvSetMmThreadCharacteristics(TEXT("Pro Audio"))).
        # We DELIBERATELY do NOT do that for Lyra's port.  Reason:
        # Thetis's DSP runs in C threads without the Python GIL, so
        # elevating the mixer doesn't starve DSP.  In Lyra all
        # threads (DSP worker, audio mixer, EP2 writer) are Python
        # and share the GIL.  Elevating the mixer above the DSP
        # worker has the OS preferentially schedule the mixer
        # whenever both are runnable, but BOTH need the GIL to do
        # any work -- the DSP worker ends up with less wall time and
        # falls behind real-time.  Field-measured at v0.0.9.6 round
        # 14: MMCSS-elevated mixer caused DSP audio output to drop
        # to ~85% of nominal rate (40.7 kHz instead of 48 kHz);
        # operator heard pulsing/stumbling on PC Sound.  Reverted.
        #
        # The mixer thread runs at default priority and paces itself
        # via the per-stream Ready semaphore (which is what makes
        # the cadence right anyway -- the priority class doesn't
        # matter for accuracy, only for jitter under load).

        outbuf = np.zeros((self._outsize, 2), dtype=np.float32)
        n = self._n_inputs

        while not self._stop_event.is_set():
            # ── Wait for all active streams to be ready ──────────
            # v0.0.9.6 simplification: only stream 0 active.  Wait
            # on its semaphore.  When v0.1 RX2 enables stream 1,
            # this becomes a wait-on-all loop -- the natural Python
            # equivalent of WaitForMultipleObjects(...,TRUE,...) is
            # to acquire each active semaphore in turn.  Order
            # doesn't matter for "all" semantics: if any stream is
            # behind, the acquire blocks until it catches up, while
            # the others' tokens just queue.
            active_ready = []
            for i in range(n):
                if self._active[i]:
                    active_ready.append(self._ready[i])
            if not active_ready:
                # No active streams; wait briefly then re-check.
                # Avoid busy-looping when no DSP is feeding.
                self._stop_event.wait(timeout=0.010)
                continue
            for sem in active_ready:
                sem.acquire()
                if self._stop_event.is_set():
                    return

            # ── Pull outsize samples per active stream ───────────
            # For v0.0.9.6 with one stream, the "mix" is a
            # passthrough copy.  v0.1 RX2 will sum two streams with
            # per-stream pan / volume, and slewing on activation
            # transitions per Thetis xaamix().
            with self._lock:
                # Currently only stream 0; loop kept for future.
                for i in range(n):
                    if not self._active[i]:
                        continue
                    ring = self._ring[i]
                    outidx = self._outidx[i]
                    first = min(self._outsize, self._ring_size - outidx)
                    if i == 0:
                        # First active stream initializes outbuf.
                        outbuf[:first] = ring[outidx:outidx + first]
                        if self._outsize > first:
                            outbuf[first:] = ring[:self._outsize - first]
                    else:
                        # Additional streams sum into outbuf.
                        # v0.1 RX2 lands here with per-stream pan.
                        outbuf[:first] += ring[outidx:outidx + first]
                        if self._outsize > first:
                            outbuf[first:] += ring[:self._outsize - first]
                    self._outidx[i] = (
                        outidx + self._outsize) % self._ring_size

            # ── Dispatch via outbound (lockstep gate for HL2) ────
            with self._outbound_lock:
                cb = self._outbound
            try:
                cb(outbuf)
            except Exception as exc:  # noqa: BLE001
                # An outbound error must NEVER crash the mixer
                # thread -- it's the spine of audio output.  Log
                # and continue; subsequent frames will retry.
                _log.warning(
                    "AudioMixer outbound raised %r; dropping frame",
                    exc)
                self.outbound_drops += 1
                continue
            self.frames_dispatched += 1
