"""IQ-domain captured-noise-profile engine (§14.6, v0.0.9.9).

Pre-WDSP spectral subtraction in the complex-IQ domain.  Replaces
the legacy v1 audio-domain captured-profile path that was retired
with the WDSP audio engine in v0.0.9.6 (see CLAUDE.md §14.6 for
the full architectural history including the three failed
post-WDSP attempts).

**Capture path** — operator presses 📷 Cap; for ``seconds`` of
band noise, every IQ block in ``Radio._do_demod_wdsp`` flows
through :meth:`accumulate`, which STFTs the IQ at the operator's
native rate and accumulates per-bin ``|FFT(iq_frame)|`` values.
The averaged magnitude spectrum becomes the operator's "what does
this band's noise look like at my QTH right now" reference.

**Apply path** — operator selects "use captured profile" in
Settings → DSP → Noise reference; every subsequent IQ block flows
through :meth:`apply` BEFORE ``_wdsp_rx.process(iq)``, which
applies a Wiener-from-profile gain mask to the IQ spectrum, IFFTs
back to time domain, and hands cleaned IQ to WDSP.  Because the
subtraction happens BEFORE WDSP's AGC, it sidesteps the
AGC-mismatch that broke three rounds of post-WDSP audio-domain
attempts in v0.0.9.6.

**Why this works at the IQ layer:**

* IQ noise is mode-independent — same baseband noise pattern
  regardless of whether the operator demods USB, LSB, CW, AM, or
  FM.  One captured profile per band per radio rate covers every
  mode operator would tune through on that band.
* WDSP's AGC sees the cleaned IQ and adjusts its loop gain to the
  reduced noise floor, so AGC pumping is reduced as a side
  benefit.
* Phase is preserved exactly — the Wiener gain mask is real-
  valued (a per-bin scalar magnitude factor) and multiplies
  complex bins by a real number, so demod phase coherence
  downstream is unaffected.

**Algorithm:** weighted overlap-add (WOLA) STFT with sqrt-Hann
analysis and synthesis windows at 50% overlap, providing exact
constant-overlap-add (COLA = 1) reconstruction:

* anal_window = sqrt(Hann), synth_window = sqrt(Hann)
* anal × synth = Hann
* sum_k Hann[n - k·hop] at hop=N/2 = 1 ⇒ perfect reconstruction
  of unmodified signals.

Wiener-from-profile gain (per bin per frame):

.. math::

    G[k] = \\max(\\text{floor}, 1 - \\frac{P[k]}{|F[k]|})

where :math:`P[k]` is the captured-profile magnitude at bin
:math:`k` and :math:`F[k]` is the current frame's complex
spectrum.  ``mask_floor_db`` controls the minimum gain (default
-12 dB) — too aggressive a floor produces musical-noise
artifacts; too loose forfeits the noise reduction benefit.

**Latency:** ``fft_size - hop = fft_size/2`` samples of pipeline
delay through the apply path (one overlap region).  At 192 kHz
IQ with fft_size=2048, that's ~5.3 ms — well below
operator-perceptible.  WDSP's audio comes out 5.3 ms later than
without the pre-pass; AGC and demod chain are otherwise
unaffected.

This module is pure DSP — no Qt, no Lyra-state coupling.  Callable
from any thread that owns the instance.  NOT thread-safe across
multiple owners (don't call ``apply()`` from one thread while
another calls ``accumulate()`` on the same instance).
"""
from __future__ import annotations

from typing import Optional

import numpy as np


class CapturedProfileIQ:
    """IQ-domain captured noise profile capture + apply engine.

    Single instance per WDSP RX channel.  When v0.1 RX2 lands, RX1
    and RX2 each get their own ``CapturedProfileIQ`` (one per WDSP
    channel) so operators can have band-specific QTH profiles for
    each receiver simultaneously.

    State machine:

    * ``"idle"``      — no profile loaded, no capture running
    * ``"capturing"`` — accumulating per-bin magnitudes
    * ``"ready"``     — profile loaded; :meth:`apply` will subtract

    The state machine is internal — operator-visible state in the
    UI ("Cap" button countdown, profile name badge) reads from
    Lyra's higher-level state; this class just owns the underlying
    DSP plumbing.
    """

    DEFAULT_FFT_SIZE: int = 2048
    """Default analysis FFT size.  Selected for ~94 Hz bin width
    at 192 kHz IQ rate (good middle ground between resolution and
    CPU cost).  Operator can override via Settings → DSP →
    Captured Profile in v0.0.9.9+."""

    DEFAULT_FLOOR_DB: float = -12.0
    """Default Wiener mask floor in dB.  Too aggressive (e.g.
    -inf) produces musical-noise artifacts as the gain mask
    flickers between full pass and full kill; too loose (e.g.
    -3 dB) forfeits most of the noise-reduction benefit.  -12 dB
    is the textbook starting point for spectral subtraction."""

    def __init__(self,
                 rate_hz: int,
                 fft_size: int = DEFAULT_FFT_SIZE,
                 hop: Optional[int] = None) -> None:
        """Construct an IQ-domain capture/apply engine.

        Args:
            rate_hz: IQ sample rate.  Stored as metadata for
                profile compatibility checks; the algorithm itself
                is rate-agnostic but profiles captured at one
                rate aren't usable at another (different bin
                structure).
            fft_size: STFT FFT size.  Power-of-two recommended
                (typical 1024 / 2048 / 4096) for fast numpy.fft.
            hop: STFT hop size.  Defaults to ``fft_size // 2``,
                which gives sqrt-Hann WOLA exact reconstruction
                (the textbook 50% overlap with sqrt-window pair).
                Other hop values are accepted but COLA-1 will be
                approximate, producing minor amplitude ripple
                across frame boundaries.
        """
        self.rate_hz = int(rate_hz)
        self.fft_size = int(fft_size)
        self.hop = int(hop) if hop is not None else self.fft_size // 2
        if self.fft_size <= 0 or self.hop <= 0 or self.hop > self.fft_size:
            raise ValueError(
                f"invalid fft_size/hop: {self.fft_size}/{self.hop}")

        # Periodic Hann (length-N), then sqrt for use as both
        # analysis and synthesis windows.  At hop=N/2,
        # ``sum_k (sqrt_hann × sqrt_hann)[n - k·hop] = sum_k Hann
        # = 1`` for every n in steady state — exact perfect
        # reconstruction of unmodified signals (mathematically
        # verified, see bench script).
        n = np.arange(self.fft_size)
        hann = 0.5 - 0.5 * np.cos(2.0 * np.pi * n / self.fft_size)
        # sqrt(Hann) has zeros at the endpoints (where Hann = 0),
        # which is fine — the algorithm doesn't divide by the
        # window anywhere.
        self._window = np.sqrt(hann).astype(np.float32)

        # Separate input ring buffers for capture vs apply
        # pipelines.  Phase 2 review found that sharing one
        # ``_in_buf`` between :meth:`accumulate` and :meth:`apply`
        # corrupts both during the operator's stated "capture
        # while listening" use case in §14.6 (apply pipeline keeps
        # the previous profile active while accumulate fills a
        # new one — both touch the same ring, doubling the input
        # and producing spectral artifacts).  Two rings, one per
        # pipeline, makes the two methods independent and
        # reorderable.
        self._apply_in_buf = np.zeros(0, dtype=np.complex64)
        self._capture_in_buf = np.zeros(0, dtype=np.complex64)

        # Output overlap-add buffer (complex64) — holds the
        # ``fft_size - hop`` samples of overlap from the most
        # recent apply frame.  These get added to the next
        # frame's leading region.  Initial value is zeros
        # (algorithm warmup state — first apply() call's
        # leading hop samples will be at half amplitude until
        # the second frame contributes).
        self._out_overlap = np.zeros(
            self.fft_size - self.hop, dtype=np.complex64)

        # Output buffer that callers drain.  Separate from the
        # overlap buffer because produced samples may queue up
        # across multiple apply() calls before they're emitted
        # (variable-length input means a single call may produce
        # 0+ frames worth of output).
        self._out_ring = np.zeros(0, dtype=np.complex64)

        # Capture state machine.
        self._state: str = "idle"
        self._capture_accum: Optional[np.ndarray] = None
        self._capture_frames_target: int = 0
        self._capture_frames_done: int = 0

        # Loaded profile (one float32 array per channel — full
        # complex-FFT magnitude spectrum, length fft_size).
        # ``None`` while in "idle" or mid-capture states.
        self._profile_mag: Optional[np.ndarray] = None

    # ── State property ─────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def has_profile(self) -> bool:
        return self._profile_mag is not None

    def progress(self) -> tuple[str, float]:
        """Return ``(state, fraction_complete)``.

        For UI capture progress bars.  ``fraction_complete`` is
        0.0 in the idle state, advances toward 1.0 during capture,
        and is 1.0 in the ready state."""
        if self._state == "capturing":
            frac = (self._capture_frames_done
                    / max(1, self._capture_frames_target))
            return ("capturing", float(min(1.0, frac)))
        if self._state == "ready":
            return ("ready", 1.0)
        return ("idle", 0.0)

    def captured_profile_array(self) -> Optional[np.ndarray]:
        """Return a copy of the captured profile magnitudes, or
        ``None`` if no profile is loaded.  Caller can pass the
        copy to :func:`noise_profile_store.make_profile_from_capture`
        to package it for save_profile()."""
        if self._profile_mag is None:
            return None
        return self._profile_mag.copy()

    @property
    def last_capture_duration_sec(self) -> float:
        """Length in seconds of the most recently armed capture
        (``begin_capture(seconds)`` argument, derived from the
        stored frame target and rate).

        Returns 0.0 when no capture has been armed yet, or when
        the engine state is ``"idle"`` (e.g. after
        :meth:`clear_profile`).  Used by the persistence layer
        to stamp the duration into the saved profile JSON."""
        if self._capture_frames_target <= 0:
            return 0.0
        return float(self._capture_frames_target * self.hop
                     / self.rate_hz)

    def reset_streaming_state(self) -> None:
        """Drop the input/output ring buffers, overlap state, AND
        cancel any in-progress capture.

        Called on rate change, channel close+reopen, or whenever
        the IQ stream has a discontinuity that would otherwise
        bleed stale samples into the apply or capture pipelines.

        A capture cannot survive a rate change — bin structure is
        rate-specific (§14.6 design decision).  So if the operator
        was capturing when this fires, the capture is cancelled and
        any partial accumulator is dropped.  Profile (if loaded)
        is preserved.
        """
        self._apply_in_buf = np.zeros(0, dtype=np.complex64)
        self._capture_in_buf = np.zeros(0, dtype=np.complex64)
        self._out_overlap = np.zeros(
            self.fft_size - self.hop, dtype=np.complex64)
        self._out_ring = np.zeros(0, dtype=np.complex64)
        # cancel_capture handles state transition + accumulator
        # drop; no-op if not currently capturing.
        self.cancel_capture()

    # ── Capture API ────────────────────────────────────────────────

    def begin_capture(self, seconds: float) -> None:
        """Arm the accumulator for an ``seconds``-long capture.

        Subsequent :meth:`accumulate` calls will run the STFT on
        each block until ``seconds`` worth of frames have been
        accumulated; the running mean becomes the new profile
        and state advances to ``"ready"``.

        If a capture is already in progress, the call is a no-op
        — UI should disable the Cap button while state ==
        ``"capturing"``.

        ``seconds`` is clamped to a minimum of one frame; very
        short captures produce overfit profiles that subtract too
        aggressively.  Operator UI typically clamps to 1.0..5.0 s.
        """
        if self._state == "capturing":
            return
        # Frame count = how many ``hop``-spaced frames fit in
        # ``seconds`` of audio at our IQ rate.  Round up to
        # ensure we get at least the requested duration.
        frames = max(1, int(round(
            float(seconds) * self.rate_hz / self.hop)))
        # float64 accumulator — capture can run for thousands of
        # frames, float32 sums lose precision at that scale.
        self._capture_accum = np.zeros(self.fft_size, dtype=np.float64)
        self._capture_frames_target = frames
        self._capture_frames_done = 0
        # Clear the capture-side input buffer so the first frame
        # is built from purely-new IQ (no stale tail from prior
        # capture state).  Apply-side buffer is independent and
        # untouched — apply continues running through capture if
        # a profile was already loaded ("re-capture while
        # listening").
        self._capture_in_buf = np.zeros(0, dtype=np.complex64)
        self._state = "capturing"

    def cancel_capture(self) -> None:
        """Abort an in-progress capture.

        State returns to ``"ready"`` if a profile was already
        loaded, ``"idle"`` otherwise.  Existing profile (if any)
        is preserved.  No-op if no capture is in progress.
        """
        if self._state != "capturing":
            return
        self._capture_accum = None
        self._capture_frames_target = 0
        self._capture_frames_done = 0
        # Clear the capture-side input ring so a subsequent
        # begin_capture() doesn't see partial samples from this
        # cancelled run.  Apply-side ring is independent.
        self._capture_in_buf = np.zeros(0, dtype=np.complex64)
        self._state = "ready" if self.has_profile else "idle"

    def accumulate(self, iq_block: np.ndarray) -> None:
        """Feed an IQ block into the capture accumulator.

        No-op unless state == ``"capturing"``.  Caller passes the
        same IQ blocks they'd pass to :meth:`apply`; the
        accumulator manages frame alignment via an internal ring
        buffer.

        When the target frame count is reached, the running mean
        is finalized into ``_profile_mag``, state advances to
        ``"ready"``, and any leftover IQ in the ring buffer is
        discarded (it would have been stale next-capture data
        anyway).
        """
        if self._state != "capturing":
            return
        iq = np.asarray(iq_block, dtype=np.complex64)
        if iq.size == 0:
            return
        # Append to capture-side ring buffer (independent of
        # apply-side buffer so this method can be called per
        # block alongside apply() without buffer collision).
        self._capture_in_buf = np.concatenate([self._capture_in_buf, iq])
        # Process all available complete frames.
        while (self._capture_in_buf.size >= self.fft_size
               and self._state == "capturing"):
            frame = self._capture_in_buf[:self.fft_size]
            spectrum = np.fft.fft(frame * self._window)
            # ``self._capture_accum`` is float64 by construction
            # so the in-place += is safe even with thousands of
            # frames.
            self._capture_accum += np.abs(spectrum)
            self._capture_frames_done += 1
            if self._capture_frames_done >= self._capture_frames_target:
                # Finalize and transition to ready.  Drop leftover
                # buffered IQ — it would be stale by the time the
                # next capture is armed anyway.
                mean_mag = (self._capture_accum
                            / self._capture_frames_done)
                self._profile_mag = mean_mag.astype(np.float32)
                self._capture_accum = None
                self._capture_in_buf = np.zeros(0, dtype=np.complex64)
                self._state = "ready"
                return
            # Advance by hop samples.
            self._capture_in_buf = self._capture_in_buf[self.hop:]

    # ── Profile load/clear (called by Radio when loading from disk) ─

    def load_profile(self, profile_mag: np.ndarray) -> None:
        """Load a profile magnitude array (e.g. from disk via
        :func:`noise_profile_store.load_profile`) into the
        applier.

        Raises ``ValueError`` if the array length doesn't match
        ``fft_size`` — caller is responsible for FFT-size and
        rate matching before calling (typically via
        :meth:`ProfileMeta.is_compatible`).
        """
        mag = np.asarray(profile_mag, dtype=np.float32)
        if mag.size != self.fft_size:
            raise ValueError(
                f"profile has {mag.size} bins, expected "
                f"{self.fft_size} (full complex FFT magnitude)")
        self._profile_mag = mag.copy()
        # If we were mid-capture, profile-load takes precedence —
        # explicit operator load action.
        if self._state == "capturing":
            self.cancel_capture()
        self._state = "ready"

    def clear_profile(self) -> None:
        """Drop the loaded profile.  Apply path becomes a
        passthrough.  Streaming state (input ring, overlap) is
        preserved so downstream WDSP doesn't see a discontinuity.
        """
        self._profile_mag = None
        self._state = "idle"

    # ── Apply API ──────────────────────────────────────────────────

    def apply(self,
              iq_block: np.ndarray,
              mask_floor_db: float = DEFAULT_FLOOR_DB,
              ) -> np.ndarray:
        """Apply Wiener-from-profile spectral subtraction to an
        IQ block.

        Returns IQ samples (complex64, regardless of input
        dtype).  **Length contract**:

        * If no profile is loaded, returns ``iq_block`` as-is
          (cast to complex64) — passthrough.
        * If a profile is loaded, returns ALL output samples
          currently ready to leave the pipeline.  This may be
          shorter, equal to, or LONGER than ``len(iq_block)``
          depending on input chunk size and frame alignment:

          - Sub-hop calls (``len(iq_block) < hop``) typically
            return zero new samples (frames not yet ready) but
            may flush previously-pending output.
          - Full-frame calls return roughly ``len(iq_block)``
            samples in steady state.
          - The very first call after construction or
            :meth:`reset_streaming_state` returns one
            ``fft_size - hop`` fewer samples while the overlap
            buffer fills (algorithm warmup).

        Total bytes balance in steady state.  Callers should NOT
        assume ``len(out) == len(in)`` per call.

        Pass an **empty array** to drain pending output without
        feeding new input — useful for end-of-stream flush.

        **Re-capture semantics:** if :meth:`begin_capture` is
        called while a profile is already loaded, ``apply`` keeps
        running with the OLD profile until the new capture
        finalizes (state ``"capturing"`` does not pause apply).
        Operator hears clean audio throughout.  When state
        advances to ``"ready"`` the new profile takes over on the
        next ``apply`` call.

        Args:
            iq_block: complex IQ samples (any complex dtype;
                cast to complex64 internally).
            mask_floor_db: minimum gain in dB.  Default -12 dB.
                Values stricter than -24 dB risk musical noise;
                values looser than -6 dB forfeit most of the
                noise-reduction benefit.

        Wiener-from-profile gain (per bin per frame):

            G[k] = max(floor_lin, 1 - profile_mag[k] / |frame_mag[k]|)

        Real-valued gain ⇒ complex bins are scaled by a real
        number ⇒ phase preserved exactly ⇒ WDSP's downstream
        demod stays correct.
        """
        iq = np.asarray(iq_block, dtype=np.complex64)
        # Passthrough when no profile is loaded.
        if self._profile_mag is None:
            return iq.copy()

        # Empty input → just drain whatever's pending.  The
        # apply-loop below correctly does nothing on empty input
        # (no new frames to process), so we just fall through to
        # the drain-all-ring exit.

        # Append input to apply-side ring buffer.  Capture-side
        # buffer (in :meth:`accumulate`) is independent.
        if iq.size:
            self._apply_in_buf = np.concatenate(
                [self._apply_in_buf, iq])

        # Convert dB floor to linear once.
        floor_lin = float(np.float32(10.0 ** (mask_floor_db / 20.0)))

        # Process all available complete frames.
        produced_chunks: list[np.ndarray] = []
        while self._apply_in_buf.size >= self.fft_size:
            frame = self._apply_in_buf[:self.fft_size]
            # Analysis: window then FFT.
            windowed = frame * self._window
            spectrum = np.fft.fft(windowed)
            # Wiener-from-profile gain mask.  Defensive epsilon
            # in the denominator — very early in capture / on
            # zero-input the frame magnitude can be zero on some
            # bins; we don't want a NaN to propagate into WDSP.
            frame_mag = np.abs(spectrum)
            gain = 1.0 - self._profile_mag / np.maximum(
                frame_mag, np.float32(1e-12))
            # Clamp gain to [floor_lin, 1.0].  Negative values
            # appear when profile_mag > frame_mag (current frame's
            # noise floor is below the captured profile's
            # average) — clamp to floor_lin.  Values > 1 are
            # impossible by construction (profile_mag/frame_mag
            # is non-negative and we subtract from 1) but the
            # clip caps them defensively.
            gain = np.clip(gain, floor_lin, 1.0).astype(np.float32)
            # Apply gain (real scalar per bin → preserves phase).
            modified = spectrum * gain
            # Synthesis: IFFT then window.
            time_domain = (np.fft.ifft(modified).astype(np.complex64)
                           * self._window)
            # Overlap-add into the persistent overlap buffer.
            # First (fft_size - hop) samples of this frame sum
            # with the previous frame's tail.
            overlap_len = self.fft_size - self.hop
            time_domain[:overlap_len] = (
                time_domain[:overlap_len] + self._out_overlap)
            # The first `hop` samples of the now-summed frame are
            # finalized output.  The remaining `overlap_len`
            # samples become the new overlap buffer for the next
            # frame to add into.
            produced_chunks.append(time_domain[:self.hop].copy())
            self._out_overlap = time_domain[self.hop:].copy()
            # Advance input ring by hop samples.
            self._apply_in_buf = self._apply_in_buf[self.hop:]

        # Move the new chunks into the output ring.
        if produced_chunks:
            new_out = np.concatenate(produced_chunks)
            self._out_ring = np.concatenate(
                [self._out_ring, new_out])

        # Drain ALL pending output samples (P0 #2 fix from Phase
        # 2 review: the previous ``min(iq.size, ring.size)`` cap
        # caused unbounded ring growth when input chunks were
        # smaller than ``hop``, and stranded trailing samples at
        # end-of-stream).  The new contract: drain everything,
        # caller deals with variable-length output.
        out = self._out_ring
        self._out_ring = np.zeros(0, dtype=np.complex64)
        return out

    # ── Repr (debugging) ───────────────────────────────────────────

    def __repr__(self) -> str:
        return (f"CapturedProfileIQ(rate_hz={self.rate_hz}, "
                f"fft_size={self.fft_size}, hop={self.hop}, "
                f"state={self._state!r}, "
                f"has_profile={self.has_profile})")
