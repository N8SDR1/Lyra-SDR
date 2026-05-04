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

        # NOTE (Path C, v0.0.9.2): silence pre-fill REMOVED.
        # Earlier (timer-paced) revisions pre-filled 100 ms of
        # silence so the EP2 frame builder never woke to find an
        # empty deque between DSP worker bursts.  Path C replaces
        # the timer-paced wait with a semaphore-paced wait
        # (HL2Stream._ep2_send_sem, signaled by queue_tx_audio
        # once per 126 samples produced), which makes pre-fill
        # unnecessary BY CONSTRUCTION -- the writer cannot wake
        # unless 126 samples are already queued.  Removing the
        # pre-fill saves 100 ms of audio latency at the AK4951
        # codec output, which is otherwise stuck at the front of
        # the deque forever (drain rate == fill rate at steady
        # state, so the head never catches up).
        # If a regression appears (sustained underruns visible
        # in the status bar), revisit -- but the right fix would
        # be a smaller pre-fill (e.g. 252 samples = 5 ms = 2 EP2
        # frames), not the original 4800.

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
        # Pre-fill the ring with 100 ms of silence at startup so the
        # PortAudio callback has buffer headroom for the worker's
        # 43 ms-cadence audio bursts -- v0.0.9.1 click fix, mirrors
        # the AK4951 sink pre-fill above.  Without this, the callback
        # underruns from the moment PortAudio starts (well before the
        # worker has produced its first audio block) and continues
        # underrunning every cycle the worker burst lands just after
        # the callback poll.  Operator data: ~3 underruns/sec without
        # pre-fill = audible clicks every 300 ms.  100 ms pre-fill
        # gives ~2 worker-burst cycles of margin -- enough to absorb
        # the natural drift between burst and callback cadences.
        # Ring is already filled with zeros from np.zeros() above; we
        # just advance write_idx and count to expose those zeros as
        # "available" frames for the callback to read.
        PREFILL_MS = 100
        prefill_frames = min(
            capacity_frames, int(rate * PREFILL_MS / 1000))
        self._ring_read_idx = 0     # next frame to read by callback
        self._ring_write_idx = prefill_frames  # writer starts past pre-fill
        self._ring_count = prefill_frames     # pre-fill counted as available
        # Lock guards the three indices above. Hold time is O(N) frames
        # being copied which is a few hundred ints/floats — sub-ms even
        # in the worst case, so the audio thread waiting on it doesn't
        # stutter audibly.
        self._ring_lock = threading.Lock()

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
        with self._ring_lock:
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
            if take < frames:
                # Underrun — pad the rest with silence. This produces a
                # brief glitch rather than a device stutter.
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

        with self._ring_lock:
            free = self._ring_capacity_frames - self._ring_count
            if n > free:
                # Overrun — drop the oldest (n - free) frames by
                # advancing the read pointer. Operator hears a brief
                # discontinuity; we don't block the DSP thread.
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
        with self._ring_lock:
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
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class NullSink:
    def write(self, audio): pass
    def close(self): pass
