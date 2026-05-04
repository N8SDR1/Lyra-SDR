"""Audio output sinks: where demodulated audio goes.

Two implementations:
- AK4951Sink: packs audio into EP2 TX slots on the HL2 stream; the
  updated gateware routes these samples to the AK4951 codec line-out.
- SoundDeviceSink: outputs to PC default playback device via sounddevice
  (soft dependency; only imported when this sink is selected).
"""
from __future__ import annotations

from typing import Optional, Protocol

import numpy as np


class AudioSink(Protocol):
    def write(self, audio: np.ndarray) -> None: ...
    def close(self) -> None: ...
    # Optional stereo balance support — sinks that can address
    # left/right channels independently (PC Soundcard) honor this;
    # sinks that physically can't (AK4951 — single mono pair) ignore.
    def set_lr_gains(self, left: float, right: float) -> None: ...


class AK4951Sink:
    """Route audio to the HL2's AK4951 line-level output via EP2 TX slots.

    The AK4951 is a true STEREO codec: the EP2 audio slot has separate
    16-bit Left + Right fields, and the gateware routes both to the
    AK4951 DAC's L/R channels. So Balance is honored end-to-end — we
    apply per-channel gains here and feed (N, 2) stereo into the EP2
    queue, which packs L and R independently into the frame.

    Sink-swap cleanup: the underlying HL2Stream owns a TX audio
    queue (deque) that's NOT per-sink — it's a long-lived buffer
    shared across sink swaps. We clear it on both init AND close,
    so swapping to/from this sink doesn't leak stale samples between
    sessions ("digitized robotic" symptom: old samples + new samples
    interleaved in the EP2 frames).
    """

    def __init__(self, stream):
        self._stream = stream
        # Drain any leftover TX audio from a previous session before
        # we start enqueuing fresh samples.
        if hasattr(stream, "clear_tx_audio"):
            stream.clear_tx_audio()

        # Pre-fill the EP2 TX audio deque with a small silence cushion
        # so the EP2 builder has samples to pull during the brief
        # window between sink construction and the first producer
        # block landing.
        #
        # **v0.0.9.2 audio rebuild Commit 3:** pre-fill reduced from
        # 4800 (100 ms) to 504 (10.5 ms = 4 EP2 frames).  Real
        # backpressure (HL2Stream._tx_audio_cond) now handles cadence
        # absorption that the v0.0.9.1 100 ms pre-fill was
        # compensating for; the small startup cushion is purely to
        # avoid an underrun on the first 1-2 EP2 frames before the
        # producer thread has scheduled its first batch.  Net
        # latency from operator action to speaker drops by ~90 ms
        # (100 -> 10 ms pre-fill).
        #
        # Why not zero pre-fill?  The EP2 builder fires immediately
        # on stream start -- often before the DSP worker has had a
        # chance to schedule and produce its first audio block.
        # Without any pre-fill, the first 4-8 EP2 frames pull from
        # an empty deque and zero-pad with silence (= audible
        # startup click on the codec).  504 samples gives the
        # producer ~10 ms of slack to wake up, well within Python
        # scheduler latency on any modern machine.
        if hasattr(stream, "_tx_audio_lock") and hasattr(stream, "_tx_audio"):
            # Pre-fill = 1008 = 2 producer batches = same as
            # backpressure high-water.  Producer's first push lands
            # the deque at ~1008+ → backpressure engages immediately
            # → producer paced to consumer rate from frame zero.
            # ~21 ms startup latency.  v0.0.9.2 Commit 3 fixup.
            PREFILL_SAMPLES = 1008
            silence = [(0.0, 0.0)] * PREFILL_SAMPLES
            cond = getattr(stream, "_tx_audio_cond", None)
            if cond is not None:
                with cond:
                    stream._tx_audio.extend(silence)
                    cond.notify_all()
            else:
                # Fallback for any code path not yet using the
                # condition (defensive; production paths all use it).
                with stream._tx_audio_lock:
                    stream._tx_audio.extend(silence)

        self._stream.inject_audio_tx = True
        # Stereo balance gains. Default = equal-power center
        # (cos/sin at π/4 = √2/2 each). Updated by Radio whenever the
        # operator moves the Balance slider, exactly like SoundDeviceSink.
        self._left_gain = 0.7071067811865476
        self._right_gain = 0.7071067811865476

    def write(self, audio: np.ndarray) -> None:
        if audio.size == 0:
            return
        # Two input shapes are accepted:
        #   - mono (N,) — duplicated to L/R, then per-channel balance
        #     applied. Default audio chain produces this shape.
        #   - stereo (N, 2) — already L/R-distinct (e.g., BIN
        #     pseudo-binaural is on). Balance gains apply column-wise.
        if audio.ndim == 2 and audio.shape[1] == 2:
            stereo = audio.astype(np.float32, copy=False)
            stereo = stereo * np.array(
                [self._left_gain, self._right_gain], dtype=np.float32)
        else:
            mono = audio.astype(np.float32).reshape(-1)
            # When the operator hasn't touched Balance both gains are
            # √2/2 ≈ 0.707, so the AK4951 hears the same audio in both
            # ears as the legacy mono-duplicated path did.
            l = mono * self._left_gain
            r = mono * self._right_gain
            stereo = np.stack((l, r), axis=1)            # (N, 2)
        self._stream.queue_tx_audio(stereo)

    def set_lr_gains(self, left: float, right: float) -> None:
        """Update the L/R channel gains. Called by Radio whenever the
        operator changes the Balance slider; same contract as
        SoundDeviceSink. Equal-power pan law lives in
        Radio.balance_lr_gains which feeds this."""
        self._left_gain = float(left)
        self._right_gain = float(right)

    def close(self) -> None:
        # Quiet-pass v0.0.7.1 (audio_pops_audit P0.3): apply a brief
        # fade-out before disabling EP2 audio injection.  Pre-fix this
        # method flipped ``inject_audio_tx`` instantly, which made the
        # AK4951's audio L/R bytes jump from real samples to zero in
        # one EP2 frame (~2.6 ms cadence) — operator heard a click on
        # every sink swap.
        #
        # Sequence:
        #   1. Replace the queued audio with a 5 ms linear fade tail.
        #      EP2 builder pulls these as the next samples while the
        #      operator-perceived audio gracefully decays to zero.
        #   2. Sleep ~7 ms so the EP2 thread has time to pull and
        #      send the faded samples (at 380 Hz EP2 cadence × 126
        #      samples/frame, 7 ms ≈ 2.7 frames = 336 audio samples,
        #      comfortably more than 240 fade samples).
        #   3. Disable injection — subsequent EP2 frames carry zero
        #      audio bytes, but the AK4951 has just heard a clean
        #      fade so there's nothing to click against.
        #   4. Clear any stragglers (defensive — fade_and_replace_tx_
        #      audio already dropped the long tail, but the EP2 thread
        #      might have missed pulling a few samples if it was
        #      busy when we slept).
        FADE_MS = 5.0
        DRAIN_BUFFER_MS = 2.0
        if hasattr(self._stream, "fade_and_replace_tx_audio"):
            queued = self._stream.fade_and_replace_tx_audio(
                fade_ms=FADE_MS)
            if queued > 0:
                import time
                time.sleep((FADE_MS + DRAIN_BUFFER_MS) / 1000.0)
        self._stream.inject_audio_tx = False
        # Clear the queue on close so the NEXT sink (PC Soundcard
        # or another AK4951 instance) starts from a known empty
        # state. Without this, residual samples in the deque continue
        # being pulled by EP2 framing for up to ~1 second.
        # ``clear_tx_audio`` also notify_all()'s any producer held at
        # the v0.0.9.2 backpressure gate so it wakes and exits the
        # wait promptly.  The HL2Stream itself stays alive across
        # sink swaps; only the underlying HL2Stream.stop() (which
        # this code does NOT call) sets the persistent shutdown
        # flag.  See HL2Stream.shutdown_tx_audio for that path.
        if hasattr(self._stream, "clear_tx_audio"):
            self._stream.clear_tx_audio()


class SoundDeviceSink:
    """Route audio to the PC default playback device — non-blocking.

    Key design choices (documented because they matter for Windows
    audio interfaces, USB multichannel cards, and S/PDIF outputs):

    - **Callback-based, never blocks the caller.** Earlier versions
      used PortAudio's blocking `write()` API. When the OS audio
      buffer filled (which happens randomly on Windows: USB scheduling
      hiccups, exclusive-mode grabs by other apps, driver state),
      `write()` would block the calling thread for tens of ms — up to
      ~65 ms in the field. With our DSP path producing a write call
      every ~10 ms of audio, even one stall meant the DSP thread fell
      behind real-time. Stalls compounded into the visible "drag" bug
      where the spectrum, waterfall, and slider input all became
      sluggish for many seconds at a time.
      The fix: route audio through a thread-safe ring buffer, fed by
      the DSP thread (write()) and drained by PortAudio's audio
      callback thread. write() never blocks. If the ring overflows
      (DSP outpaces device), we drop the oldest audio rather than
      stall the caller. If the ring underflows (callback fires faster
      than DSP fills), we emit silence rather than stutter the device.
      Both events are counted and rate-limited to console for tuning.

    - **Prefers WASAPI over MME.** PortAudio's system default on
      Windows is MME (20+ years old, flaky with S/PDIF and USB audio
      interfaces, silently drops mono frames on some drivers). We
      explicitly pick the WASAPI host API's default output device
      when the caller didn't specify one. WASAPI is what every
      serious modern audio app on Windows uses (DAWs, SDR clients,
      browsers).

    - **Opens stereo, writes duplicated mono.** The demod chain is
      mono (SSB/CW/AM/FM/DIGU all produce a single audio channel).
      S/PDIF / TOSLINK outputs are rigidly 2-channel and some drivers
      silently drop mono frames instead of auto-duplicating — so we
      always open stereo and duplicate the mono sample into both L
      and R. Harmless on analog outputs (which would have duplicated
      anyway).
    """

    # Ring buffer capacity in seconds of audio. 200 ms gives the DSP
    # thread plenty of cushion to absorb a 100 ms OS audio stall
    # without dropping, while keeping operator-perceived latency
    # acceptable (worst case = 200 ms of post-event tail). Sized at
    # init time from rate so this works the same at any future rate.
    _RING_SECONDS = 0.200

    def __init__(self, rate: int = 48000, device: Optional[int] = None,
                 blocksize: int = 0):
        try:
            import sounddevice as sd
        except ImportError as e:
            raise RuntimeError(
                "sounddevice is not installed. `pip install sounddevice` "
                "or switch the audio output to AK4951."
            ) from e
        import threading
        self._sd = sd
        self._rate = rate

        if device is None:
            device = self._pick_wasapi_default(sd)

        self._channels = 2
        # Stereo balance gains. Default = equal-power center
        # (cos/sin at π/4 = √2/2 each). Updated by Radio whenever the
        # operator moves the Balance slider.
        self._left_gain = 0.7071067811865476
        self._right_gain = 0.7071067811865476

        # ── Ring buffer (frames × channels, float32) ────────────────
        capacity_frames = max(1024, int(rate * self._RING_SECONDS))
        self._ring_capacity_frames = capacity_frames
        self._ring = np.zeros(
            (capacity_frames, self._channels), dtype=np.float32)
        # Pre-fill = 1008 frames at 48 kHz (~21 ms) = same as
        # backpressure high-water.  Producer immediately sees a
        # full buffer and engages backpressure from frame zero.
        # v0.0.9.2 Commit 3 fixup (was 504 = 1 producer batch in
        # initial Commit 3; raised to 2 producer batches to match
        # the high-water bump).
        PREFILL_FRAMES = min(capacity_frames, max(1008, rate // 50))
        self._ring_read_idx = 0     # next frame to read by callback
        self._ring_write_idx = PREFILL_FRAMES  # writer starts past pre-fill
        self._ring_count = PREFILL_FRAMES     # pre-fill counted as available
        # Lock + condition for producer/consumer backpressure.
        # The PortAudio callback (consumer) cannot block on the
        # condition -- if it did, the audio device would underrun at
        # the OS level (callback timing is hard real-time).  So
        # consumer-side stays drop-oldest-on-underrun (silence pad)
        # but the PRODUCER side now waits when the ring is full,
        # preventing the producer from racing the consumer and
        # silently dropping samples.
        self._ring_lock = threading.Lock()
        self._ring_cond = threading.Condition(self._ring_lock)
        # Backpressure target: producer waits when count >= this.
        # Sized at 2 producer batches (1008 frames at 48 kHz =
        # ~21 ms) so producer has jitter tolerance before the
        # ring drains.  v0.0.9.2 Commit 3 fixup: was 504; raised
        # to 1008 to match the new producer batch size of 504.
        self._ring_high_water = min(
            capacity_frames, max(1008, rate // 50))
        # Set when sink is closing so any waiting producer can exit.
        self._ring_shutdown: bool = False
        # Producer-wait counter (telemetry mirror of HL2Stream's).
        self._producer_waits: int = 0

        # Diagnostic counters — incremented inside the lock so they
        # stay coherent with read/write activity. Printed periodically
        # by _maybe_print_stats so the operator (and we) can see if
        # the ring is sized correctly for their machine.
        self._overruns: int = 0     # write() had to drop oldest frames
        self._underruns: int = 0    # callback ran out of data, padded silence
        self._frames_written: int = 0
        self._frames_read: int = 0
        import time as _t
        self._stats_last_print = _t.monotonic()

        # Open in CALLBACK mode — passing `callback=` switches PortAudio
        # to non-blocking; write() will never be called on the stream
        # itself. blocksize=0 lets PortAudio pick its optimal size for
        # this device (typically 256-512 frames at 48k).
        self._stream = sd.OutputStream(
            samplerate=rate, channels=self._channels, dtype="float32",
            blocksize=blocksize, device=device,
            callback=self._audio_callback,
        )
        self._stream.start()

    @staticmethod
    def _pick_wasapi_default(sd):
        """Find the WASAPI host API's default output device. Returns a
        device index, or None if WASAPI isn't available (falls through
        to PortAudio's system default — probably MME on Windows, which
        is less reliable but not always broken).
        """
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            return None
        for i, ha in enumerate(hostapis):
            if ha["name"] == "Windows WASAPI":
                default_out = ha.get("default_output_device", -1)
                if default_out >= 0:
                    return default_out
                return None
        return None

    def _audio_callback(self, outdata, frames, time_info, status):
        """PortAudio audio-thread callback — fill `outdata` with the
        next `frames` frames from the ring buffer.

        Runs on a high-priority audio thread (NOT the DSP/main thread).
        Must be fast and must NOT raise. If the ring is short, fill the
        tail with silence rather than blocking — a brief glitch is
        always better than stuttering or hanging the device.
        """
        with self._ring_cond:
            avail = self._ring_count
            take = min(avail, frames)
            if take > 0:
                # Copy `take` frames out of the ring, handling wrap-around.
                end = self._ring_read_idx + take
                if end <= self._ring_capacity_frames:
                    outdata[:take] = self._ring[
                        self._ring_read_idx:end]
                else:
                    n1 = self._ring_capacity_frames - self._ring_read_idx
                    outdata[:n1] = self._ring[self._ring_read_idx:]
                    outdata[n1:take] = self._ring[:take - n1]
                self._ring_read_idx = (
                    self._ring_read_idx + take) % self._ring_capacity_frames
                self._ring_count -= take
                self._frames_read += take
                # Wake producer (DSP thread) waiting on backpressure.
                # This is the signal half of the producer/consumer
                # handshake: consumer pulled frames, producer can now
                # push more if it was held at high-water.
                self._ring_cond.notify_all()
            if take < frames:
                # Underrun — pad the rest with silence. Real-time
                # audio callback can't block on the condition, so
                # we accept the underrun event and fill silence.
                # With Commit 3 backpressure active this should be
                # rare (only on catastrophic producer stalls).
                outdata[take:] = 0.0
                self._underruns += 1

    def write(self, audio: np.ndarray) -> None:
        """Non-blocking write. Prepares stereo float32, applies balance
        gains, then enqueues into the ring buffer. If the ring is full
        the oldest frames are dropped (operator hears a brief glitch
        rather than seeing the entire UI freeze)."""
        if audio.size == 0:
            return
        # Two input shapes are accepted (see AK4951Sink.write for
        # rationale): mono (N,) or stereo (N, 2). BIN feeds the
        # stereo path; everything else hits the mono path.
        if audio.ndim == 2 and audio.shape[1] == 2:
            a = audio.astype(np.float32, copy=False) * np.array(
                [self._left_gain, self._right_gain], dtype=np.float32)
        else:
            mono = audio.astype(np.float32).reshape(-1)
            # Stereo build with per-channel balance gains applied.
            # When the operator hasn't moved the Balance slider both
            # gains are √2/2 (equal-power center) and the result is
            # the same audio in both ears as before the balance feature
            # existed.
            l = mono * self._left_gain
            r = mono * self._right_gain
            a = np.stack((l, r), axis=1)
        # Ensure C-contiguous (N, 2) float32 — np.stack already is, the
        # explicit cast handles the rare path where a came pre-shaped
        # but in F-order or with a non-float32 dtype slipped through.
        if not (a.dtype == np.float32 and a.flags["C_CONTIGUOUS"]):
            a = np.ascontiguousarray(a, dtype=np.float32)
        n = a.shape[0]

        with self._ring_cond:
            # Backpressure (v0.0.9.2 audio rebuild Commit 3).
            # Wait until ring count drops below high-water before
            # pushing more.  Bounded wait (50 ms) so a stuck consumer
            # can't deadlock the producer; if the wait expires, fall
            # through to drop-oldest behavior as before.
            waited = False
            while (self._ring_count >= self._ring_high_water
                   and not self._ring_shutdown):
                waited = True
                self._ring_cond.wait(timeout=0.050)
            if waited:
                self._producer_waits += 1
            if self._ring_shutdown:
                # Sink is closing -- drop the samples cleanly.
                return

            free = self._ring_capacity_frames - self._ring_count
            if n > free:
                # Overrun — drop the oldest (n - free) frames by
                # advancing the read pointer. Should be rare with
                # backpressure active; counter still increments so
                # we can see if the wait timeout was hit.
                drop = n - free
                self._ring_read_idx = (
                    self._ring_read_idx + drop) % self._ring_capacity_frames
                self._ring_count -= drop
                self._overruns += 1
            # Copy `a` into the ring at write_idx, handling wrap-around.
            end = self._ring_write_idx + n
            if end <= self._ring_capacity_frames:
                self._ring[self._ring_write_idx:end] = a
            else:
                n1 = self._ring_capacity_frames - self._ring_write_idx
                self._ring[self._ring_write_idx:] = a[:n1]
                self._ring[:n - n1] = a[n1:]
            self._ring_write_idx = (
                self._ring_write_idx + n) % self._ring_capacity_frames
            self._ring_count += n
            self._frames_written += n
            # Wake consumer (callback thread) if it's waiting on a
            # starved buffer.  The callback doesn't actually wait,
            # but notify is cheap and idempotent.
            self._ring_cond.notify_all()
        self._maybe_print_stats()

    def _maybe_print_stats(self) -> None:
        """Print a one-line ring-buffer status every 10 seconds IF
        any overruns or underruns occurred since the last print. Stays
        silent in healthy operation so we don't spam the console."""
        import time as _t
        now = _t.monotonic()
        if (now - self._stats_last_print) < 10.0:
            return
        if self._overruns == 0 and self._underruns == 0:
            self._stats_last_print = now
            return
        # Snapshot under lock for a coherent read of all counters.
        with self._ring_cond:
            ov = self._overruns
            un = self._underruns
            wr = self._frames_written
            rd = self._frames_read
            self._overruns = 0
            self._underruns = 0
        elapsed = now - self._stats_last_print
        self._stats_last_print = now
        print(f"[Lyra audio] SoundDeviceSink ring: "
              f"overruns={ov} underruns={un} "
              f"in {elapsed:.1f}s "
              f"(written={wr}, read={rd}). "
              f"overruns mean DSP outpaced device (rare glitches expected); "
              f"underruns mean device pulled faster than DSP fed.")

    def set_lr_gains(self, left: float, right: float) -> None:
        """Update the L/R channel gains. Called by Radio whenever
        the operator changes the Balance slider. Values are floats
        in [0, 1]; equal-power pan law lives in Radio.balance_lr_gains
        which feeds this."""
        self._left_gain = float(left)
        self._right_gain = float(right)

    def close(self) -> None:
        # Wake any producer waiting on backpressure so it can exit
        # cleanly instead of blocking until the 50 ms timeout.
        # Set BEFORE stopping the PortAudio stream so the producer's
        # "is_shutdown" check fires before any subsequent write tries
        # to push into a torn-down stream.
        try:
            with self._ring_cond:
                self._ring_shutdown = True
                self._ring_cond.notify_all()
        except Exception:
            pass
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class NullSink:
    def write(self, audio): pass
    def close(self): pass
