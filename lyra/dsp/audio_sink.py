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


# ── Host API enumeration (v0.0.9.6) ─────────────────────────────────
#
# PortAudio supports multiple "host APIs" on Windows for talking to
# audio hardware: MME, DirectSound, WASAPI (shared / exclusive),
# WDM-KS, ASIO.  Each has different latency / reliability / sharing
# characteristics — see docs/architecture/audio_architecture.md or
# the operator-facing tooltip in Settings → Audio.
#
# Lyra exposes the choice to the operator (matching Thetis's
# Settings → Audio → Driver) because no single API is "best" across
# all hardware.  Some operators on USB audio devices prefer WASAPI
# Shared; others want WDM-KS for lowest latency without exclusive
# device lock; others installed ASIO drivers and want that path.
#
# The keys here are operator-facing labels (used in QSettings + UI
# combo).  Internal code translates from label to PortAudio host
# API index + sounddevice extra_settings.

# Supported display labels.  Order = preferred display order in
# the Settings combo.
HOST_API_LABEL_AUTO            = "Auto"
HOST_API_LABEL_WASAPI_SHARED   = "WASAPI shared"
HOST_API_LABEL_WASAPI_EXCLUSIVE = "WASAPI exclusive"
HOST_API_LABEL_WDM_KS          = "WDM-KS"
HOST_API_LABEL_DIRECTSOUND     = "DirectSound"
HOST_API_LABEL_MME             = "MME"
HOST_API_LABEL_ASIO            = "ASIO"

# Map labels -> PortAudio host API name strings as reported by
# sounddevice.query_hostapis().  Used to find the host API index
# at sink-open time.  None means "no specific host API" (Auto).
_LABEL_TO_PA_NAME: dict[str, Optional[str]] = {
    HOST_API_LABEL_AUTO:             None,
    HOST_API_LABEL_WASAPI_SHARED:    "Windows WASAPI",
    HOST_API_LABEL_WASAPI_EXCLUSIVE: "Windows WASAPI",
    HOST_API_LABEL_WDM_KS:           "Windows WDM-KS",
    HOST_API_LABEL_DIRECTSOUND:      "Windows DirectSound",
    HOST_API_LABEL_MME:              "MME",
    HOST_API_LABEL_ASIO:             "ASIO",
}


def enumerate_host_apis() -> list[dict]:
    """Return a list of available audio host APIs on this system.

    Each entry is a dict with keys:
      * label (str) — operator-facing name (see HOST_API_LABEL_*)
      * pa_name (str) — PortAudio name as reported by query_hostapis
      * pa_index (int) — index into sounddevice.query_hostapis()
      * default_output_device (int) — default output device index
        for this host API, or -1 if none
      * device_count (int) — number of output devices on this API
      * available (bool) — True if the API is reachable on this
        system (PortAudio enumerated it AND it has at least one
        output device)
      * exclusive_mode (bool) — True for WASAPI exclusive variant

    Always includes an "Auto" entry first (PortAudio's system
    default).  Entries are sorted preference order: Auto, then
    WASAPI shared, exclusive, WDM-KS, DirectSound, MME, ASIO.

    Defensive: returns just [Auto] if sounddevice import or
    query fails — caller can still construct SoundDeviceSink
    in Auto mode.
    """
    result: list[dict] = []
    # Auto is always offered — it's just "let sounddevice pick."
    result.append({
        "label": HOST_API_LABEL_AUTO,
        "pa_name": None,
        "pa_index": -1,
        "default_output_device": -1,
        "device_count": -1,
        "available": True,
        "exclusive_mode": False,
    })
    try:
        import sounddevice as sd
    except Exception:
        return result
    try:
        hostapis = sd.query_hostapis()
    except Exception:
        return result

    # Build a label-ordered output list, matching available APIs.
    # Each PortAudio name might map to multiple labels (e.g., WASAPI
    # → both shared and exclusive).
    label_order = [
        HOST_API_LABEL_WASAPI_SHARED,
        HOST_API_LABEL_WASAPI_EXCLUSIVE,
        HOST_API_LABEL_WDM_KS,
        HOST_API_LABEL_DIRECTSOUND,
        HOST_API_LABEL_MME,
        HOST_API_LABEL_ASIO,
    ]
    # Count devices per host API by walking sd.query_devices() —
    # ``device_count`` isn't reliably exposed in older sounddevice
    # versions on the host API dict, so we count manually.
    device_counts: dict[int, int] = {}
    try:
        for dev in sd.query_devices():
            ha_idx = int(dev.get("hostapi", -1))
            max_out = int(dev.get("max_output_channels", 0))
            if ha_idx >= 0 and max_out > 0:
                device_counts[ha_idx] = device_counts.get(ha_idx, 0) + 1
    except Exception:
        pass

    for label in label_order:
        pa_name = _LABEL_TO_PA_NAME.get(label)
        if pa_name is None:
            continue
        for idx, ha in enumerate(hostapis):
            if ha.get("name", "") == pa_name:
                default_out = int(ha.get("default_output_device", -1))
                dev_count = device_counts.get(idx, 0)
                # Available if there's at least one output device.
                # default_output_device may be -1 even when devices
                # exist (rare but observed); treat that as available
                # since we can fall back to None and let PortAudio
                # pick.
                available = dev_count > 0
                result.append({
                    "label": label,
                    "pa_name": pa_name,
                    "pa_index": idx,
                    "default_output_device": default_out,
                    "device_count": dev_count,
                    "available": available,
                    "exclusive_mode": (
                        label == HOST_API_LABEL_WASAPI_EXCLUSIVE),
                })
                break
    return result


class AudioSink(Protocol):
    def write(self, audio: np.ndarray) -> None: ...
    def close(self) -> None: ...
    # Optional stereo balance support — sinks that can address
    # left/right channels independently (PC Soundcard) honor this;
    # sinks that physically can't (AK4951 — single mono pair) ignore.
    def set_lr_gains(self, left: float, right: float) -> None: ...


class AK4951Sink:
    """Route audio to the HL2's AK4951 line-level output via EP2 TX slots.

    v0.0.9.6 architecture (Thetis-mirror):
        DSP worker -> AudioMixer.add_input -> mixer thread ->
        AK4951Sink._lockstep_outbound -> HL2 EP2 deque -> EP2 writer

    The AK4951 is a true STEREO codec: the EP2 audio slot has separate
    16-bit Left + Right fields, and the gateware routes both to the
    AK4951 DAC's L/R channels. So Balance is honored end-to-end — we
    apply per-channel gains here on the producer side (write(), which
    runs on the DSP worker) and feed (N, 2) stereo into the AudioMixer.
    The mixer thread then dispatches 126-sample chunks to our
    ``_lockstep_outbound`` callback, which pushes to the HL2 deque,
    signals the EP2 writer, and BLOCKS until the writer has actually
    sent the packet.  The lockstep wait is the gate that produces
    steady 380.95 Hz wire cadence -- mirroring Thetis's
    ``WaitForSingleObject(prn->hobbuffsRun[1], INFINITE)`` pattern in
    ``ChannelMaster\\network.c::WriteUDPFrame`` for the HERMES audio
    codec path.

    Sink-swap cleanup: the underlying HL2Stream owns a TX audio
    queue (deque) that's NOT per-sink — it's a long-lived buffer
    shared across sink swaps. We clear it on both init AND close,
    so swapping to/from this sink doesn't leak stale samples between
    sessions ("digitized robotic" symptom: old samples + new samples
    interleaved in the EP2 frames).
    """

    def __init__(self, stream, mixer=None):
        self._stream = stream
        self._mixer = mixer
        self._closed = False
        # Drain any leftover TX audio from a previous session before
        # we start enqueuing fresh samples.
        if hasattr(stream, "clear_tx_audio"):
            stream.clear_tx_audio()
        # Drain any stale lockstep tokens from a previous sink life.
        try:
            while stream._lockstep_slot.acquire(blocking=False):  # noqa: SLF001
                pass
        except Exception:
            pass

        self._stream.inject_audio_tx = True
        # Stereo balance gains. Default = equal-power center
        # (cos/sin at π/4 = √2/2 each). Updated by Radio whenever the
        # operator moves the Balance slider, exactly like SoundDeviceSink.
        self._left_gain = 0.7071067811865476
        self._right_gain = 0.7071067811865476

        # v0.0.9.6 round 16: AudioMixer thread is OPTIONAL (mixer
        # may be None).  When None, fall back to the round-9
        # legacy direct path: write() pushes straight to HL2's
        # _tx_audio deque + signals EP2 writer, no separate mixer
        # thread, no lockstep gate.  This matches operator-tested
        # clean PC Sound baseline.  Lockstep wire-cadence pacing
        # comes back via WaitableTimerEx HIGH_RESOLUTION inside
        # the EP2 writer (TODO v0.0.9.6) -- no extra Python thread.
        if self._mixer is not None:
            self._mixer.set_outbound(self._lockstep_outbound)

    def write(self, audio: np.ndarray) -> None:
        """DSP worker producer side -- non-blocking.

        With mixer attached: applies L/R gains, pushes (N, 2) stereo
        into the mixer's input ring; mixer thread dispatches at codec
        cadence to ``_lockstep_outbound`` below.

        Without mixer (round-9 legacy path): applies L/R gains and
        pushes directly to HL2Stream._tx_audio deque, signaling the
        EP2 writer.  No separate audio mixer thread.
        """
        if audio.size == 0 or self._closed:
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

        if self._mixer is not None:
            # Mixer-routed path: push to mixer (fast, non-blocking;
            # copies into ring and releases per-stream Ready semaphore
            # once per 126 samples).  Mixer thread dispatches.
            self._mixer.add_input(0, stereo)
        else:
            # Legacy direct path (round-9 baseline): push straight to
            # HL2Stream's TX queue.  EP2 writer drains as before.
            self._stream.queue_tx_audio(stereo)

    def _lockstep_outbound(self, samples_lr: np.ndarray) -> None:
        """Mixer thread consumer side -- BLOCKS in lockstep.

        Called by the AudioMixer thread once per 126-sample output
        chunk with a (126, 2) float32 array of (L, R) pairs.  Pushes
        to the HL2 EP2 deque, signals the EP2 writer there's an
        audio frame ready, then waits on ``_lockstep_slot`` until
        the writer has sent the packet.  This is the wire-cadence
        pacer: while we're waiting, the mixer thread is paused, so
        the next dispatch can't run until the wire has caught up.

        Mirrors Thetis ``ChannelMaster\\network.c::WriteUDPFrame``
        lines 1316-1322 (the L-R producer side of the lockstep
        handshake).
        """
        if self._closed:
            return
        n = samples_lr.shape[0]
        # Convert ndarray rows to the deque's tuple format.  At
        # 126 rows × ~380 Hz = ~48k tuples/sec; small Python overhead
        # vs bulk numpy.  list-comp + tolist is faster than per-row
        # tuple() calls (timed at ~25 us per 126-row batch).
        arr_list = samples_lr.tolist()  # list of [l, r] lists
        pairs = [(row[0], row[1]) for row in arr_list]
        with self._stream._tx_audio_lock:  # noqa: SLF001
            self._stream._tx_audio.extend(pairs)  # noqa: SLF001
        # Signal the EP2 writer there's a 126-sample frame available.
        # The writer's drain path will release ``_lockstep_slot``
        # after sendto() returns.
        self._stream._ep2_send_sem.release()  # noqa: SLF001
        # Block until writer has sent the packet (lockstep gate).
        # This is what produces steady 380.95 Hz wire cadence.
        self._stream._lockstep_slot.acquire()  # noqa: SLF001

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
        # ── v0.0.9.6 lockstep-aware close ───────────────────────────
        # Mark closed FIRST so any in-flight outbound() returns
        # early instead of racing the teardown.  Our _lockstep_outbound
        # checks self._closed before doing any work.
        #
        # We deliberately do NOT call self._mixer.set_outbound(None)
        # here.  The Radio's sink-swap path constructs the NEW sink
        # (which calls mixer.set_outbound(self._lockstep_outbound)
        # in __init__) AFTER calling old_sink.close() -- so by the
        # time the new sink takes over, our close has already run.
        # If we cleared the outbound here, we'd race-clobber the
        # new sink's just-installed callback when the swap order
        # ever inverts (which it does when the new sink is built
        # before the old is closed).  The mixer naturally moves
        # past us once the new sink calls set_outbound; until that
        # happens, our outbound returns immediately due to _closed.
        self._closed = True
        # Release the lockstep slot a few times to unblock any
        # in-flight mixer outbound that's waiting on it.  The
        # mixer's outbound will return immediately because _closed
        # is True; the next mixer iteration will use the new
        # (null) outbound.  Stale tokens left in the semaphore are
        # harmless -- the next AK4951Sink (or future re-attach)
        # drains them in __init__ before the first outbound.
        try:
            for _ in range(4):
                self._stream._lockstep_slot.release()  # noqa: SLF001
        except Exception:
            pass

        # Quiet-pass v0.0.7.1 (audio_pops_audit P0.3): apply a brief
        # fade-out before disabling EP2 audio injection.  Pre-fix this
        # method flipped ``inject_audio_tx`` instantly, which made the
        # AK4951's audio L/R bytes jump from real samples to zero in
        # one EP2 frame (~2.6 ms cadence) — operator heard a click on
        # every sink swap.
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

    def __init__(self, mixer=None, rate: int = 48000,
                 device: Optional[int] = None,
                 blocksize: int = 0, *,
                 use_rate_match: bool = True,
                 host_api_label: str = HOST_API_LABEL_AUTO):
        """Construct the PC sound card output.

        v0.0.9.6: ``use_rate_match`` enables the WDSP-derived
        adaptive resampler (lyra.dsp.rmatch + lyra.dsp.varsamp)
        that absorbs the inevitable clock drift between Lyra's
        nominal 48 kHz IQ rate and the sound card's actual rate
        (HL2 crystal vs. sound card crystal — typically differ by
        50-100 ppm).  Default ON; can be disabled for diagnostic
        A/B via ``use_rate_match=False``.

        v0.0.9.6: ``host_api_label`` selects which PortAudio host
        API to use.  Defaults to "Auto" (PortAudio's system
        default — historically WASAPI shared on Windows).  Other
        labels include "WASAPI exclusive" (bypasses Windows audio
        engine, locks device), "WDM-KS" (kernel streaming, low
        latency without lock), "MME" (legacy, very compatible),
        etc.  See enumerate_host_apis() for the discoverable list.
        Falls back to "Auto" if the requested API isn't available
        on this system.
        """
        try:
            import sounddevice as sd
        except ImportError as e:
            raise RuntimeError(
                "sounddevice is not installed. `pip install sounddevice` "
                "or switch the audio output to HL2 audio jack."
            ) from e
        import threading
        self._sd = sd
        self._mixer = mixer
        self._closed = False
        self._rate = rate
        self._use_rate_match = bool(use_rate_match)
        self._host_api_label = str(host_api_label)

        # Resolve the host API + device choice.  If operator picked
        # something specific, use that; otherwise let PortAudio pick
        # via the legacy WASAPI-default heuristic.
        host_api_info = self._resolve_host_api(sd, self._host_api_label)
        self._resolved_host_api_label = host_api_info["label"]
        if device is None:
            if host_api_info["pa_index"] >= 0:
                device = host_api_info["default_output_device"]
                if device < 0:
                    device = None
            else:
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
        # v0.0.9.6: track RMatch's internal underflow counter so the
        # callback can mirror increments into our counter (RMatch
        # owns the truth post-rate-match-enabled).
        self._rmatch_last_under: int = 0
        import time as _t
        self._stats_last_print = _t.monotonic()

        # Open in CALLBACK mode — passing `callback=` switches PortAudio
        # to non-blocking; write() will never be called on the stream
        # itself. blocksize=0 lets PortAudio pick its optimal size for
        # this device (typically 256-512 frames at 48k).
        #
        # v0.0.9.6: extra_settings carry the WASAPI-exclusive flag
        # when operator selected that label.  PortAudio host-API-
        # specific settings live in sd.WasapiSettings/WdmksSettings/
        # AsioSettings.  We only set extra_settings for WASAPI
        # exclusive currently — other APIs use defaults.
        extra_settings = None
        if host_api_info.get("exclusive_mode"):
            try:
                extra_settings = sd.WasapiSettings(exclusive=True)
            except Exception as exc:  # noqa: BLE001
                # Older sounddevice versions might not expose
                # WasapiSettings; fall back to shared mode.
                print(f"[Lyra audio] WASAPI exclusive requested but "
                      f"WasapiSettings unavailable ({exc}); using "
                      f"shared mode")
                self._resolved_host_api_label = HOST_API_LABEL_WASAPI_SHARED
        self._stream = sd.OutputStream(
            samplerate=rate, channels=self._channels, dtype="float32",
            blocksize=blocksize, device=device,
            callback=self._audio_callback,
            extra_settings=extra_settings,
        )
        # ── DO NOT start the stream yet ─────────────────────────────
        # PortAudio fires _audio_callback as soon as start() returns.
        # The callback reads ``self._rmatch`` (and any other state
        # initialized below).  If we start() before assigning
        # ``self._rmatch = None`` / building the RMatch instance,
        # the first callback fires on a partially-initialized object
        # and crashes with "AttributeError: 'SoundDeviceSink' object
        # has no attribute '_rmatch'" — observed in v0.0.9.6 round 11
        # field test when an operator switched audio sinks.  Build
        # all callback-visible state first, start() last.
        # ────────────────────────────────────────────────────────────

        # ── v0.0.9.6: WDSP-derived adaptive resampler ────────────
        #
        # Bridges the clock-drift gap between Lyra's nominal 48 kHz
        # output and the sound card's actual rate (which
        # PortAudio/WASAPI exposes via _stream.samplerate).  Without
        # this, the ring fills or drains over time as the two
        # crystals drift, producing intermittent overruns/
        # underruns.  See docs/architecture/audio_architecture.md
        # for the full reasoning + WDSP attribution.
        #
        # Mono path: rate-match before the L/R gain stage.  The
        # callback pulls mono from rmatch, then applies left/right
        # gains as it fills outdata.  This is simpler than running
        # two RMatch instances (one per channel) and gives identical
        # operator-perceived behavior since L/R gains are static
        # scalars, not signal-dependent.
        #
        # Ring sizing critical for Lyra's bursty audio worker
        # cadence: producer writes ~2048 samples every ~43 ms while
        # the WASAPI callback pulls ~256 samples every ~5 ms.
        # Between producer bursts the consumer drains the ring
        # rapidly; the ring needs enough headroom to absorb a full
        # 43-ms gap with margin.  RMatch's auto-sizing (2*1.05*insize
        # = ~4300 samples = ~90 ms) was empirically too tight on
        # operator's machine — caused 30-50 underruns/10s during
        # initial v0.0.9.6 A/B test.  Match the legacy
        # SoundDeviceSink's 200-ms ring sizing for proven headroom.
        self._rmatch = None
        if self._use_rate_match:
            try:
                from lyra.dsp.rmatch import RMatch
                actual_outrate = int(round(self._stream.samplerate))
                # 400 ms ring at the device's actual rate, half-
                # filled at startup (RMatch defaults n_ring =
                # rsize/2 = 200 ms).  Sized to absorb up to 200 ms
                # producer-thread perturbations from OS scheduling,
                # GC pauses, USB renegotiation, etc.  Operator A/B
                # in v0.0.9.6 dev tree showed bursty underrun events
                # (15-132 per 10s window) with the original 200ms
                # ring — the bursts correlate with system-level
                # audio thread pauses that exceed 100 ms, draining
                # the half-full ring before producer catches back
                # up.  Doubling the ring gives 200 ms of pause
                # headroom on top of normal cadence.
                #
                # ff_alpha=0.10 (faster than the 0.05 default) so
                # the control loop recovers var more aggressively
                # after each perturbation knocks the ring off
                # target.  20-update time constant becomes 10 —
                # ~80ms recovery at ~12 control updates/cycle
                # vs 160ms at 0.05.  Aggressive enough to catch up
                # before the next perturbation, conservative enough
                # to not overshoot.
                ring_target = int(actual_outrate * 0.400)

                # v0.0.9.6 polish (B): seed initial_var from the
                # last-known value saved on a previous Lyra session.
                # Cuts the 10-20 sec startup convergence window to
                # near-zero because varsamp starts at the locked-in
                # ratio rather than 1.0.  Stored in QSettings under
                # "audio/last_rmatch_var" — saved on graceful close
                # below.  Defaults to 1.0 if no prior value (first
                # launch, sound-card change, etc.).
                init_var = 1.0
                try:
                    from PySide6.QtCore import QSettings
                    qs = QSettings("N8SDR", "Lyra")
                    last_var = qs.value(
                        "audio/last_rmatch_var", 1.0, type=float)
                    init_var = float(last_var)
                except Exception:
                    pass

                # v0.0.9.6 polish (C): start ring 80% full instead
                # of 50% to give consumer extra headroom during the
                # first few seconds of audio.  Operator hears about
                # 320 ms of silence at startup (320/400 of the ring)
                # before live audio reaches the head, which matches
                # the typical Windows audio-engine startup latency
                # anyway — perceived as instant by the operator.
                self._rmatch = RMatch(
                    insize=2048,
                    outsize=256,
                    nom_inrate=rate,
                    nom_outrate=actual_outrate,
                    density=64,    # plenty for 50-100 ppm drift
                    ringsize=ring_target,
                    ff_alpha=0.10,
                    initial_var=init_var,
                    initial_fill_fraction=0.80,
                )
                print(f"[Lyra audio] SoundDeviceSink: rate-match "
                      f"enabled (RMatch nom_in={rate} nom_out="
                      f"{actual_outrate} ring={ring_target} "
                      f"= {ring_target * 1000 // actual_outrate}ms "
                      f"init_var={init_var:.5f} init_fill=80%)")
            except Exception as exc:  # noqa: BLE001
                print(f"[Lyra audio] SoundDeviceSink: rate-match "
                      f"init failed ({exc}); falling back to "
                      f"direct ring (drift glitches possible)")
                self._rmatch = None

        # ── Audio chain visibility (v0.0.9.3 diagnostic) ────────────
        # Log the actual device, host API, and negotiated sample rate
        # at sink open.  When operators see ring-buffer overruns we
        # need to know whether the issue is wrong device, wrong host
        # API, or shared-mode resampling -- the sounddevice query
        # APIs already have all this info, we just weren't surfacing
        # it.  One print per sink open; zero cost during audio
        # streaming.  Defensive try/except: a query failure must
        # never prevent audio from playing.
        try:
            dev_info = sd.query_devices(device) if device is not None \
                else sd.query_devices(kind="output")
            host_info = sd.query_hostapis(dev_info["hostapi"])
            actual_sr = self._stream.samplerate
            actual_latency_ms = self._stream.latency * 1000.0
            print(
                f"[Lyra audio] SoundDeviceSink: "
                f"device=[{dev_info.get('name', '?')}] "
                f"host={host_info.get('name', '?')} "
                f"api='{self._resolved_host_api_label}' "
                f"requested_rate={rate} actual_rate={actual_sr:g} "
                f"latency={actual_latency_ms:.1f}ms "
                f"channels={self._channels}"
            )
            if actual_sr != rate:
                print(
                    f"[Lyra audio] WARNING: actual stream rate "
                    f"({actual_sr:g}) differs from requested ({rate}). "
                    f"This typically indicates WASAPI shared-mode "
                    f"resampling or a device that doesn't natively "
                    f"support {rate} Hz.  Ring overruns under heavy "
                    f"signal load are likely; consider switching the "
                    f"Windows default audio device or checking "
                    f"Settings -> Audio -> Output device."
                )
        except Exception as e:  # noqa: BLE001
            # Query failure is non-fatal -- audio is not yet running.
            print(f"[Lyra audio] SoundDeviceSink: device query "
                  f"failed ({e}); starting stream anyway")

        # ── NOW start the PortAudio stream ──────────────────────────
        # All callback-visible state (``self._rmatch``, ring buffer,
        # underrun counters, etc.) is initialized above.  The very
        # first audio callback can fire any time after this returns;
        # if any callback-visible state is initialized AFTER this
        # call, an early callback will crash on the missing
        # attribute.
        # ────────────────────────────────────────────────────────────
        self._stream.start()

        # SoundDeviceSink writes direct to RMatch -- mixer is
        # bypassed entirely in PC Sound mode.  If a mixer was
        # passed (HL2 audio jack mode is reachable elsewhere in
        # the same Radio instance), clear any prior outbound so
        # leftover AK4951Sink references don't hold the mixer in
        # a broken state.  When mixer is None (round-9 mode),
        # nothing to do.
        if self._mixer is not None:
            try:
                self._mixer.set_outbound(None)
            except Exception:
                pass

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

    @staticmethod
    def _resolve_host_api(sd, label: str) -> dict:
        """Map an operator-facing host API label to a concrete
        PortAudio host API + device.  Returns a dict matching the
        enumerate_host_apis() shape.

        Falls back to "Auto" if:
          * label not recognized
          * label maps to a PortAudio host API not present on this
            system
          * label maps to an API with no usable output devices

        Always returns a dict (never None).
        """
        # Auto fast-path — no enumeration needed.
        if not label or label == HOST_API_LABEL_AUTO:
            return {
                "label": HOST_API_LABEL_AUTO,
                "pa_name": None,
                "pa_index": -1,
                "default_output_device": -1,
                "device_count": -1,
                "available": True,
                "exclusive_mode": False,
            }
        # Look up the label in the enumerate list.
        try:
            apis = enumerate_host_apis()
        except Exception:
            apis = []
        for entry in apis:
            if entry["label"] == label and entry["available"]:
                return entry
        # Fall through: label requested but unavailable.  Print a
        # warning + fall back to Auto.
        print(f"[Lyra audio] requested host API '{label}' is not "
              f"available on this system; falling back to Auto")
        return {
            "label": HOST_API_LABEL_AUTO,
            "pa_name": None,
            "pa_index": -1,
            "default_output_device": -1,
            "device_count": -1,
            "available": True,
            "exclusive_mode": False,
        }

    def _audio_callback(self, outdata, frames, time_info, status):
        """PortAudio audio-thread callback — fill `outdata` with the
        next `frames` frames.

        Runs on a high-priority audio thread (NOT the DSP/main thread).
        Must be fast and must NOT raise. If the ring is short, fill the
        tail with silence rather than blocking — a brief glitch is
        always better than stuttering or hanging the device.

        v0.0.9.6 path:
          * If rate-match is enabled (default), pull mono samples
            from RMatch (which also handles drift compensation +
            underflow recovery internally), then apply L/R gains
            to produce stereo for outdata.
          * Otherwise, fall back to the legacy ring path
            (use_rate_match=False — diagnostic A/B only).
        """
        if self._rmatch is not None:
            # Rate-matched path.  RMatch handles its own underflow
            # recovery (slewed silence-fill); we just apply L/R
            # gains and project to stereo.
            mono = self._rmatch.read(frames)
            # mono is float32 of length frames, may include slewed
            # silence on underflow.
            outdata[:, 0] = mono * self._left_gain
            outdata[:, 1] = mono * self._right_gain
            # Track underflows for the same diagnostic line the
            # legacy ring exposes.  RMatch counts them internally;
            # mirror to our counter so _maybe_print_stats sees them.
            new_under = self._rmatch.underflows
            if new_under > self._rmatch_last_under:
                self._underruns += (new_under
                                     - self._rmatch_last_under)
                self._rmatch_last_under = new_under
            self._frames_read += frames
            return

        # Legacy ring path (use_rate_match=False).  Identical to
        # pre-v0.0.9.6 behavior.
        with self._ring_lock:
            avail = self._ring_count
            take = min(avail, frames)
            if take > 0:
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
                outdata[take:] = 0.0
                self._underruns += 1

    def write(self, audio: np.ndarray) -> None:
        """DSP worker producer side -- non-blocking, direct to RMatch.

        v0.0.9.6 architectural NOTE: SoundDeviceSink does NOT route
        through AudioMixer.  Reason: PortAudio's audio callback
        thread is already pacing the wire-side cadence (pulls
        samples from RMatch at WASAPI's rate), so the mixer thread
        between DSP and RMatch would add no value -- it's pure
        overhead.  In Lyra's Python-with-GIL threading model the
        overhead is measurable: field-tested at v0.0.9.6 round 14b
        the mixer-routed path dropped DSP feed rate to 83% of nominal
        (39.3 kHz instead of 48 kHz) due to GIL contention between
        the mixer thread and DSP worker.  Direct path here avoids
        the third Python thread entirely.

        This deviates from a strict Thetis port: Thetis routes
        ASIO output through aamix too (cmasio.c).  But Thetis's
        DSP runs in C threads without GIL -- the aamix overhead is
        invisible.  Adding aamix-style mixing back here lands when
        v0.1 RX2 needs the mixing point for stereo split, by which
        time the mixer's overhead is justified by the stereo work.
        Until then: PC Sound bypass the mixer.

        AK4951Sink (HL2 audio jack) DOES go through the mixer
        because the lockstep cadence pacing it provides is
        essential for steady 380.95 Hz wire arrival at the HL2
        codec.
        """
        if audio.size == 0 or self._closed:
            return

        # Rate-matched path.  RMatch is mono-only; collapse stereo
        # input to mono by averaging (BIN audio is the only stereo
        # producer and BIN's L/R are already correlated, so averaging
        # is fine).  L/R gains apply in the audio callback.
        if self._rmatch is not None:
            if audio.ndim == 2 and audio.shape[1] == 2:
                mono = (audio[:, 0] + audio[:, 1]).astype(
                    np.float32) * 0.5
            else:
                mono = audio.astype(np.float32).reshape(-1)
            self._rmatch.write(mono)
            self._frames_written += mono.size
            self._maybe_print_stats()
            return

        # Legacy stereo-ring path (use_rate_match=False).  Identical
        # to pre-v0.0.9.6 behavior.
        if audio.ndim == 2 and audio.shape[1] == 2:
            a = audio.astype(np.float32, copy=False) * np.array(
                [self._left_gain, self._right_gain], dtype=np.float32)
        else:
            mono = audio.astype(np.float32).reshape(-1)
            l = mono * self._left_gain
            r = mono * self._right_gain
            a = np.stack((l, r), axis=1)
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
        # v0.0.9.6 lockstep-aware close: ``_closed`` flag guards any
        # in-flight _ring_outbound dispatch.  Don't touch the mixer's
        # outbound -- the new sink (next in the swap) is responsible
        # for taking over via its own set_outbound call.  See the
        # AK4951Sink.close() comment for the full rationale.
        self._closed = True

        # v0.0.9.6 polish (B): persist current rmatch var so the
        # next Lyra session starts pre-primed at the locked-in
        # ratio.  Only save if control loop ever became active
        # (control_flag=True) — otherwise we'd persist the initial
        # 1.0 which doesn't help.  Also clamp to the [0.96, 1.04]
        # range defensively so a malformed save can't break a
        # later session.
        if self._rmatch is not None and self._rmatch.control_flag:
            try:
                from PySide6.QtCore import QSettings
                qs = QSettings("N8SDR", "Lyra")
                v = max(0.96, min(1.04, float(self._rmatch.var)))
                qs.setValue("audio/last_rmatch_var", v)
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
