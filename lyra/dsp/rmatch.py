"""Adaptive PI control loop wrapping :class:`varsamp.VarSamp` —
port of WDSP rmatch.c (v0.0.9.6).

This is the layer that absorbs the slow clock drift between Lyra's
DSP-output rate and the PC sound card's actual rate.  Both are
nominally 48 kHz; in practice they differ by parts-per-million due
to crystal tolerance.  Without rate matching the ring buffer
between them fills (overrun → discard samples → click) or drains
(underrun → silence → click) over time — exactly the behavior
operators reported on Lyra v0.0.9.x PC Soundcard mode.

How it works
------------
A two-state moving-average control loop adjusts varsamp's ``var``
multiplier every input or output block:

* **Feed-forward term**: smoothed measurement of actual input/
  output sample-count ratio, multiplied by ``inv_nom_ratio``.
  Tracks the long-term clock-drift between ends.
* **Proportional term**: smoothed deviation of the ring-fill level
  from its target (``rsize/2``).  Pulls the buffer back to center
  if it's drifting toward overflow or underflow.

Combined update (``rmatch.c::control()`` lines 256-273)::

    var = feed_forward - prop_gain * av_deviation
    var = clamp(var, 0.96, 1.04)

Plus glitch-hiding extras inherited from the C original:

* Crossfade ``blend()`` on overflow — when the ring overflows
  despite the control loop, fade between the discarded and new
  samples instead of producing a discontinuity.
* Slewed silence-fill ``dslew()`` on underflow — instead of
  emitting raw zeros, fade to silence and back.

WDSP attribution
----------------
Direct port of:

    D:\\sdrprojects\\OpenHPSDR-Thetis-2.10.3.13\\Project Files\\Source\\
    wdsp\\rmatch.c

Original copyright (C) 2017, 2018, 2022 Warren Pratt, NR0V.  GPL
v2+, used under GPL v3+ relicense per
``docs/architecture/wdsp_integration.md``.

Differences from the C original
-------------------------------
* C uses Win32 ``InitializeCriticalSectionAndSpinCount`` /
  ``EnterCriticalSection`` / ``InterlockedAnd`` / ``InterlockedBitTest``
  for thread synchronization.  Python's GIL handles the same job —
  per CLAUDE.md §5 we don't add explicit locks for streaming DSP
  state.  The contract is "operator UI thread can write knobs;
  audio thread reads them; the ~one-frame staleness on toggle is
  acceptable" (see ``protocol/stream.py`` line 280 for the same
  pattern in Lyra).
* C's ``InterlockedIncrement`` for diagnostics counters becomes a
  plain Python ``+= 1`` under the GIL.
* ``startup_delay`` in the C is in seconds since stream start; in
  Python we count input/output sample totals the same way.

Tests
-----
See ``scripts/diag_rmatch_drift.py`` for the bench instrument
that drives a synthetic constant-rate-mismatch input through
RMatch and measures ring-fill convergence + steady-state ``var``.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from lyra.dsp.varsamp import VarSamp


# ── Moving-average helpers (port of MAV / AAMAV from rmatch.c) ───────


class _Mav:
    """Plain moving-average ring of integers.

    Port of ``MAV`` / ``xmav`` (rmatch.c lines 29-69).  Used for
    the ring-fill deviation smoothing in the proportional term.
    """

    def __init__(self, ringmin: int, ringmax: int, nom_value: float
                 ) -> None:
        # Ringmax must be a power of two — the C uses a bit mask.
        if ringmax & (ringmax - 1) != 0 or ringmax <= 0:
            raise ValueError(
                f"ringmax must be a power of two >= 1; got {ringmax}")
        self.ringmin = int(ringmin)
        self.ringmax = int(ringmax)
        self.nom_value = float(nom_value)
        self.ring = np.zeros(ringmax, dtype=np.int64)
        self.mask = ringmax - 1
        self.i = 0
        self.load = 0
        self.sum = 0

    def update(self, sample: int) -> float:
        """Add a sample; return current MAV value."""
        if self.load >= self.ringmax:
            self.sum -= int(self.ring[self.i])
        if self.load < self.ringmax:
            self.load += 1
        self.ring[self.i] = sample
        self.sum += int(sample)
        if self.load >= self.ringmin:
            out = float(self.sum) / float(self.load)
        else:
            out = self.nom_value
        self.i = (self.i + 1) & self.mask
        return out


class _AaMav:
    """Asymmetric-accumulator moving-average — separate pos/neg sums.

    Port of ``AAMAV`` / ``xaamav`` (rmatch.c lines 71-126).  Used
    for the feed-forward "current_ratio" calculation: positive
    values are input-sample counts (xrmatchIN), negative values
    are output-sample counts (xrmatchOUT, fed as ``-outsize``).
    The ratio neg/pos = output_count / input_count = the actual
    output/input rate ratio across the window.
    """

    def __init__(self, ringmin: int, ringmax: int, nom_ratio: float
                 ) -> None:
        if ringmax & (ringmax - 1) != 0 or ringmax <= 0:
            raise ValueError(
                f"ringmax must be a power of two >= 1; got {ringmax}")
        self.ringmin = int(ringmin)
        self.ringmax = int(ringmax)
        self.nom_ratio = float(nom_ratio)
        self.ring = np.zeros(ringmax, dtype=np.int64)
        self.mask = ringmax - 1
        self.i = 0
        self.load = 0
        self.pos = 0
        self.neg = 0

    def update(self, sample: int) -> float:
        """Add a sample (positive for input count, negative for
        output count); return current pos/neg ratio."""
        if self.load >= self.ringmax:
            old = int(self.ring[self.i])
            if old >= 0:
                self.pos -= old
            else:
                self.neg += old
        if self.load <= self.ringmax:
            self.load += 1
        self.ring[self.i] = sample
        if sample >= 0:
            self.pos += int(sample)
        else:
            self.neg -= int(sample)
        if self.load >= self.ringmin:
            if self.pos > 0:
                out = float(self.neg) / float(self.pos)
            else:
                out = self.nom_ratio
        elif self.neg > 0 and self.pos > 0:
            frac = float(self.load) / float(self.ringmin)
            out = ((1.0 - frac) * self.nom_ratio
                   + frac * (float(self.neg) / float(self.pos)))
        else:
            out = self.nom_ratio
        self.i = (self.i + 1) & self.mask
        return out


# ── Main rmatch class ────────────────────────────────────────────────


class RMatch:
    """Adaptive rate matcher with PI control loop on top of VarSamp.

    Use this when you have an audio source running at a nominal rate
    and a sink running at a different (or slowly-drifting) nominal
    rate, and you want glitch-free playback without explicit clock
    sync.  The classic ham-radio case: HL2 IQ output at "48 kHz"
    and PC sound card playback at "48 kHz" — neither crystal exactly
    on, drift accumulates over time.

    Lifecycle::

        rm = RMatch(insize=2048, outsize=512,
                    nom_inrate=48000, nom_outrate=48000)

        # In your producer loop (DSP -> ring):
        rm.write(audio_in_block)

        # In your consumer loop (ring -> sound card):
        out_block = rm.read(outsize)

        # Diagnostics:
        info = rm.diagnostics()
        # info.var, info.n_ring, info.underflows, info.overflows

    The constructor designs the FIR + ring buffer; reuse a single
    instance across stream lifetime.  ``reset()`` re-zeros streaming
    state without redesigning the filter.
    """

    # Operating range for the var multiplier — clamped both in the
    # control loop and at the varsamp boundary.  WDSP uses ±4%; we
    # use the same.  Real crystal drift is well under ±100 ppm so
    # ±4% has 400x headroom for transients.
    VAR_MIN: float = 0.96
    VAR_MAX: float = 1.04

    def __init__(
        self,
        insize: int,
        outsize: int,
        nom_inrate: int,
        nom_outrate: int,
        *,
        ringsize: int = 0,
        density: int = 256,
        startup_delay: float = 0.5,
        ff_ringmin: int = 32,
        ff_ringmax: int = 1024,    # power of two
        ff_alpha: float = 0.05,
        prop_ringmin: int = 32,
        prop_ringmax: int = 4096,  # power of two
        prop_gain: float = -1.0,   # -1 = auto-scale by ring size
        tslew: float = 0.003,
        fc_high: float = 0.0,
        fc_low: float = -1.0,
        gain: float = 1.0,
        varmode: int = 1,
        initial_var: float = 1.0,
        initial_fill_fraction: float = 0.5,
    ) -> None:
        """Construct an RMatch.

        Args:
            insize: producer's typical block size (samples per
                ``write()``).
            outsize: consumer's typical block size (samples per
                ``read()``).
            nom_inrate: nominal input rate (Hz).
            nom_outrate: nominal output rate (Hz).
            ringsize: ring buffer size (samples).  Default 0 = auto-
                size to ``2 * max(2 * insize * 1.05 * nom_ratio,
                2 * outsize)``.  Manual override only if needed.
            density: VarSamp density factor.  Default 256 (Thetis
                default, ~12-bit fractional ratio resolution).
            startup_delay: seconds to delay before the control loop
                starts active feedback.  Default 0.5 s — lets the
                producer/consumer reach steady state before we start
                tuning.  Required: the var update is meaningless
                until both streams are flowing.
            ff_ringmin / ff_ringmax: feed-forward MAV bounds.
                Defaults (32 / 1024) match Thetis IVAC.
            ff_alpha: feed-forward exponential smoothing coefficient.
                Default 0.05 — ~20-block time constant.  Lyra's
                v0.0.9.6 tuning bumped this from WDSP's 0.01
                because Lyra's typical block sizes (insize=2048,
                outsize=512) result in ~5 control updates per
                cycle, where 0.01 produced a 20-cycle oscillation
                period that resonated with the proportional swing.
                Faster ff lets the ratio lock-in dominate before
                the proportional has a chance to overshoot.
            prop_ringmin / prop_ringmax: proportional MAV bounds.
            prop_gain: proportional feedback gain.  Default -1.0
                triggers Lyra's auto-scale: ``pr_gain = 0.04 /
                rsize``, which keeps the max proportional
                contribution bounded at ~0.02 (half the var clamp
                range) regardless of block size.  Operator can
                pass a positive value to use the WDSP convention
                ``prop_gain * 48000 / nom_outrate``.
            tslew: slew/blend time on overflow/underflow (seconds).
                Default 3 ms — short enough to be inaudible, long
                enough to avoid clicks.
            fc_high / fc_low / gain: passed through to VarSamp.
            varmode: 0 = constant var per block, 1 = interpolated.
                Default 1 (interpolated).
        """
        self.insize = int(insize)
        self.outsize = int(outsize)
        self.nom_inrate = int(nom_inrate)
        self.nom_outrate = int(nom_outrate)
        self.startup_delay = float(startup_delay)
        self.varmode = int(varmode) & 1
        self.tslew = float(tslew)

        self.nom_ratio = float(nom_outrate) / float(nom_inrate)
        self.inv_nom_ratio = float(nom_inrate) / float(nom_outrate)

        # Ring sizing — same formula as rmatch.c::calc_rmatch.
        max_ring_insize = int(
            1.0 + float(insize) * (1.05 * self.nom_ratio))
        if ringsize <= 0:
            ringsize = 2 * max_ring_insize
        if ringsize < 2 * outsize:
            ringsize = 2 * outsize
        self.ringsize = int(ringsize)
        self.rsize = self.ringsize       # alias, matches C naming
        self.ring = np.zeros(self.ringsize, dtype=np.complex128)
        # v0.0.9.6: configurable initial fill.  Default 0.5 (rsize/2)
        # matches WDSP's rmatch.c.  SoundDeviceSink passes 0.5 to
        # match the 200 ms half-fill at 400 ms ring; bench/IQ-rate-
        # match callers can leave at default.  Pre-fill is silence;
        # operator hears initial-fill_fraction * ringsize / outrate
        # of silence at startup before producer fills the ring with
        # real audio — typically 100-200 ms, well below the
        # threshold where it feels delayed.
        fill_frac = max(0.1, min(0.95, float(initial_fill_fraction)))
        self.n_ring = int(self.rsize * fill_frac)
        self.iin = self.n_ring
        self.iout = 0

        # Resampler intermediate output buffer (max possible size of
        # one xvarsamp call for our insize).
        self._resout_max = max_ring_insize

        # v0.0.9.6: optional initial var lets callers pre-prime the
        # rate-match loop with the last known-good value (saved
        # from a previous Lyra session via QSettings).  Cuts the
        # 10-20 sec startup convergence window down to milliseconds
        # because varsamp starts at the right ratio rather than
        # 1.0.  Clamped to the same [0.96, 1.04] range the control
        # loop honors at runtime.
        init_var = max(self.VAR_MIN, min(self.VAR_MAX, float(initial_var)))

        # VarSamp instance.
        self.v = VarSamp(
            in_rate=nom_inrate, out_rate=nom_outrate,
            density=density, fc=fc_high, fc_low=fc_low, gain=gain,
            initial_var=init_var, varmode=varmode,
        )

        # Control-loop MAVs.
        self.ffmav = _AaMav(ff_ringmin, ff_ringmax, self.nom_ratio)
        self.propmav = _Mav(prop_ringmin, prop_ringmax, 0.0)
        self.ff_alpha = float(ff_alpha)
        # Seed feed_forward with init_var too so the proportional
        # response doesn't fight against a stale 1.0 baseline on
        # the first few control updates.
        self.feed_forward = init_var
        self.av_deviation = 0.0

        # Rate-adjusted proportional gain.  v0.0.9.6 Lyra-specific
        # tuning: WDSP's prop_gain=0.005 default oscillates at the
        # var clamps for our typical insize=2048 block sizes because
        # the deviation magnitudes scale linearly with rsize and the
        # original constant didn't compensate.  We auto-scale by
        # rsize so the maximum proportional contribution stays
        # bounded at ~half the clamp range (0.02), regardless of
        # block size.
        #
        # Math:
        #   max |av_deviation| = rsize / 2  (deviation when ring
        #                                     spans 0..rsize)
        #   target max |prop term| = 0.02   (half of 0.04 clamp,
        #                                     leaving room for ff)
        #   pr_gain = 0.02 / (rsize/2) = 0.04 / rsize
        #
        # Operator can override prop_gain with a positive value to
        # restore the WDSP-original behavior or specify a custom
        # absolute gain.  Default (-1.0) triggers the auto-scale.
        if prop_gain < 0.0:
            self.pr_gain = 0.04 / float(self.rsize)
        else:
            self.pr_gain = (float(prop_gain) * 48000.0
                            / float(nom_outrate))

        # Slew/blend curve.
        self.ntslew = int(self.tslew * float(nom_outrate))
        if self.ntslew + 1 > self.rsize // 2:
            self.ntslew = self.rsize // 2 - 1
        if self.ntslew < 1:
            self.ntslew = 1
        # Half-cosine slew shape (rmatch.c:158-162):
        # cslew[m] = 0.5 * (1 - cos(theta)), theta = m * pi / ntslew
        theta = np.arange(self.ntslew + 1) * (math.pi / self.ntslew)
        self.cslew = 0.5 * (1.0 - np.cos(theta))
        self.cslew = self.cslew.astype(np.float64)

        # Auxiliary blend buffer for overflow recovery.
        self.baux = np.zeros(self.ringsize // 2, dtype=np.complex128)
        self.dlast = np.array([0.0 + 0.0j], dtype=np.complex128)
        self.ucnt = -1

        # Startup-delay sample counters.  Control loop is dormant
        # until both readsamps and writesamps cross their respective
        # thresholds.
        self.read_startup = int(float(nom_outrate) * startup_delay)
        self.write_startup = int(float(nom_inrate) * startup_delay)
        self.readsamps = 0
        self.writesamps = 0
        self.control_flag = False

        # Diagnostics.
        self.underflows = 0
        self.overflows = 0
        self.var = init_var
        self.force = False
        self.fvar = 1.0

    # ── Control loop ─────────────────────────────────────────────

    def _control(self, change: int) -> None:
        """Update ``var`` based on the rmatch.c lines 256-273
        control law.

        Called from inside ``write()`` (with ``change = +insize``)
        and ``read()`` (with ``change = -outsize``).  The ffmav
        accumulates these as positive=input, negative=output, and
        emits the running ratio = output_count / input_count.
        """
        # Feed-forward: smoothed input/output ratio.
        current_ratio = self.ffmav.update(int(change))
        current_ratio *= self.inv_nom_ratio
        self.feed_forward = (self.ff_alpha * current_ratio
                             + (1.0 - self.ff_alpha) * self.feed_forward)

        # Proportional: smoothed ring-fill deviation from target
        # (rsize/2).
        deviation = int(self.n_ring) - int(self.rsize // 2)
        self.av_deviation = self.propmav.update(deviation)

        # PI-like update + clamp.
        self.var = self.feed_forward - self.pr_gain * self.av_deviation
        if self.var > self.VAR_MAX:
            self.var = self.VAR_MAX
        elif self.var < self.VAR_MIN:
            self.var = self.VAR_MIN

    # ── Glitch-hiding helpers ────────────────────────────────────

    def _blend(self) -> None:
        """Crossfade the first ntslew+1 ring samples after iout
        with the saved baux.  rmatch.c::blend lines 275-283.

        Called after an overflow drops a region of the ring; baux
        contains the samples that *were* there before the drop.
        Crossfade hides the discontinuity.
        """
        for i in range(self.ntslew + 1):
            j = (self.iout + i) % self.rsize
            cs = self.cslew[i]
            self.ring[j] = cs * self.ring[j] + (1.0 - cs) * self.baux[i]

    def _upslew(self, newsamps: int) -> None:
        """Apply upslew envelope to the next newsamps from iin
        (rmatch.c::upslew lines 285-298)."""
        i = 0
        j = self.iin
        while self.ucnt >= 0 and i < newsamps:
            self.ring[j] *= self.cslew[self.ntslew - self.ucnt]
            self.ucnt -= 1
            i += 1
            j = (j + 1) % self.rsize

    def _dslew(self) -> None:
        """Slewed silence fill on underflow (rmatch.c::dslew
        lines 364-425)."""
        if self.n_ring > self.ntslew + 1:
            i = (self.iout + (self.n_ring - (self.ntslew + 1))) % self.rsize
            j = self.ntslew
            k = self.ntslew + 1
            n = self.n_ring - (self.ntslew + 1)
        else:
            i = self.iout
            j = self.ntslew
            k = self.n_ring
            n = 0

        # Fade existing tail from current value to silence.
        while k > 0 and j >= 0:
            if k == 1:
                self.dlast[0] = self.ring[i]
            self.ring[i] *= self.cslew[j]
            i = (i + 1) % self.rsize
            j -= 1
            k -= 1
            n += 1

        # Fill remaining with dlast * cslew (continues fade).
        while j >= 0:
            self.ring[i] = self.dlast[0] * self.cslew[j]
            i = (i + 1) % self.rsize
            j -= 1
            n += 1

        # Pad to outsize with zeros.
        zeros = self.outsize - n
        if zeros > 0:
            for _ in range(zeros):
                self.ring[i] = 0.0
                i = (i + 1) % self.rsize
            n += zeros

        self.n_ring = n
        self.iin = (self.iout + self.n_ring) % self.rsize

    # ── Public producer/consumer API ─────────────────────────────

    def write(self, audio_in: np.ndarray) -> int:
        """Producer entry point — pushes ``insize`` samples in,
        resamples via varsamp, deposits result in the ring.  Returns
        the number of new samples actually deposited.

        rmatch.c::xrmatchIN lines 300-362.
        """
        if audio_in.size != self.insize:
            # Not strictly required but matches C behavior; loose
            # consumers should chunk properly.
            pass

        # Resample with current var.
        var = self.fvar if self.force else self.var
        resout = self.v.process(audio_in, var=var)
        newsamps = int(resout.size)
        if newsamps == 0:
            return 0

        # Promote to complex128 for the ring (varsamp returns the
        # input dtype).
        if not np.iscomplexobj(resout):
            resout = resout.astype(np.complex128) + 0.0j
        else:
            resout = resout.astype(np.complex128, copy=False)

        # Update n_ring; check for overflow.
        self.n_ring += newsamps
        ovfl = self.n_ring - self.rsize
        if ovfl > 0:
            self.overflows += 1
            self.n_ring = self.rsize
            # Save baux for crossfade.
            slew_len = self.ntslew + 1
            if slew_len > self.rsize - self.iout:
                first = self.rsize - self.iout
                second = slew_len - first
            else:
                first = slew_len
                second = 0
            self.baux[:first] = self.ring[
                self.iout:self.iout + first]
            if second > 0:
                self.baux[first:first + second] = self.ring[:second]
            self.iout = (self.iout + ovfl) % self.rsize

        # Write resout into the ring at iin (with wrap-around).
        if newsamps > self.rsize - self.iin:
            first = self.rsize - self.iin
            second = newsamps - first
        else:
            first = newsamps
            second = 0
        self.ring[self.iin:self.iin + first] = resout[:first]
        if second > 0:
            self.ring[:second] = resout[first:first + second]

        if self.ucnt >= 0:
            self._upslew(newsamps)

        self.iin = (self.iin + newsamps) % self.rsize

        if ovfl > 0:
            self._blend()

        # Startup-delay tracking + control loop kick-in.  Use the
        # ACTUAL input block size, not the configured ``insize``,
        # so the ffmav ratio reflects reality when callers pass
        # variable-sized blocks (typical for SoundDeviceSink).
        actual_insize = int(audio_in.size)
        if not self.control_flag:
            self.writesamps += actual_insize
            if (self.readsamps >= self.read_startup
                    and self.writesamps >= self.write_startup):
                self.control_flag = True

        if self.control_flag:
            self._control(actual_insize)

        return newsamps

    def read(self, outsize: Optional[int] = None) -> np.ndarray:
        """Consumer entry point — pulls ``outsize`` samples from
        the ring.  If insufficient samples available, fills with
        slewed silence (underflow recovery).  Returns the output
        block.

        rmatch.c::xrmatchOUT lines 427-467.
        """
        n = self.outsize if outsize is None else int(outsize)

        if self.n_ring < n:
            self._dslew()
            self.ucnt = self.ntslew
            self.underflows += 1

        # Pull from ring with wrap-around.
        if n > self.rsize - self.iout:
            first = self.rsize - self.iout
            second = n - first
        else:
            first = n
            second = 0
        out = np.empty(n, dtype=np.complex128)
        out[:first] = self.ring[self.iout:self.iout + first]
        if second > 0:
            out[first:first + second] = self.ring[:second]
        self.iout = (self.iout + n) % self.rsize
        self.n_ring -= n
        self.dlast[0] = out[n - 1]

        if not self.control_flag:
            self.readsamps += n
            if (self.readsamps >= self.read_startup
                    and self.writesamps >= self.write_startup):
                self.control_flag = True

        if self.control_flag:
            self._control(-n)

        # Project to real (audio default) — Lyra's audio sink wants
        # float32 mono.  Caller can use read_complex() for I/Q.
        return out.real.astype(np.float32, copy=False)

    def read_complex(self, outsize: Optional[int] = None) -> np.ndarray:
        """Like :meth:`read` but returns complex128 output (preserves
        the I/Q form of the internal ring).  Useful for IQ stream
        rate matching."""
        n = self.outsize if outsize is None else int(outsize)
        # Re-implement read() but skip the projection.  Refactoring
        # to share is possible but the method body is short.
        if self.n_ring < n:
            self._dslew()
            self.ucnt = self.ntslew
            self.underflows += 1
        if n > self.rsize - self.iout:
            first = self.rsize - self.iout
            second = n - first
        else:
            first = n
            second = 0
        out = np.empty(n, dtype=np.complex128)
        out[:first] = self.ring[self.iout:self.iout + first]
        if second > 0:
            out[first:first + second] = self.ring[:second]
        self.iout = (self.iout + n) % self.rsize
        self.n_ring -= n
        self.dlast[0] = out[n - 1]
        if not self.control_flag:
            self.readsamps += n
            if (self.readsamps >= self.read_startup
                    and self.writesamps >= self.write_startup):
                self.control_flag = True
        if self.control_flag:
            self._control(-n)
        return out

    # ── Operator/diagnostic API ──────────────────────────────────

    def diagnostics(self) -> dict:
        """Return current control-loop state — for status displays
        and bench-test harnesses.

        Keys:
          * ``underflows`` (int): cumulative ring underflows
          * ``overflows`` (int): cumulative ring overflows
          * ``var`` (float): current rate multiplier (~1.0)
          * ``ringsize`` (int): ring buffer total capacity
          * ``n_ring`` (int): current ring fill (samples)
          * ``feed_forward`` (float): smoothed FF term
          * ``av_deviation`` (float): smoothed P term
          * ``control_active`` (bool): True after startup_delay
        """
        return {
            "underflows": int(self.underflows),
            "overflows": int(self.overflows),
            "var": float(self.var),
            "ringsize": int(self.ringsize),
            "n_ring": int(self.n_ring),
            "feed_forward": float(self.feed_forward),
            "av_deviation": float(self.av_deviation),
            "control_active": bool(self.control_flag),
        }

    def force_var(self, var: float) -> None:
        """Pin the rate multiplier to ``var`` and bypass the control
        loop.  Diagnostic / bench-test only.  Pass None to ``unforce``
        and let the control loop resume."""
        self.fvar = float(max(self.VAR_MIN, min(self.VAR_MAX, var)))
        self.force = True

    def unforce_var(self) -> None:
        """Resume normal control-loop operation after ``force_var``."""
        self.force = False

    def reset(self) -> None:
        """Clear streaming state without re-designing filters.  Use
        on stream restart / rate change."""
        self.ring[:] = 0.0
        self.baux[:] = 0.0
        self.dlast[:] = 0.0
        self.n_ring = self.rsize // 2
        self.iin = self.rsize // 2
        self.iout = 0
        self.var = 1.0
        self.feed_forward = 1.0
        self.av_deviation = 0.0
        self.readsamps = 0
        self.writesamps = 0
        self.control_flag = False
        self.underflows = 0
        self.overflows = 0
        self.ucnt = -1
        self.v.reset()


# ── Native (cffi) implementation ─────────────────────────────────────


class WdspRMatch:
    """Adaptive rate matcher backed by the bundled WDSP DSP engine.

    Public API matches :class:`RMatch` (insize/outsize/nom_inrate/nom_
    outrate constructor args, ``write`` / ``read`` / ``reset`` / diag
    properties) so callers can swap implementations transparently.

    Why this class exists
    ---------------------
    The pure-Python :class:`RMatch` runs varsamp's per-input-sample
    polyphase convolution in a Python ``for`` loop with NumPy.  At
    Lyra's typical 48 kHz sound-card rate, that loop fires 48,000
    times per second per RMatch instance — the dominant CPU cost on
    the PC Soundcard audio path (~2× HL2-jack-mode CPU per operator
    measurement, v0.0.9.6 dev cycle).  The bundled WDSP DLL ships
    the exact same algorithm in optimized C with CRITICAL_SECTION-
    based locking, GIL-free; calling into it via cffi recovers the
    bulk of that overhead.

    Threading
    ---------
    The DLL implements internal CRITICAL_SECTION around ring + var
    state, so concurrent ``write`` + ``read`` from different threads
    is safe.  Diagnostic readers (``var``, ``underflows`` etc.) use
    ``getRMatchDiags`` which takes the same locks.

    Tuning gaps vs Python implementation
    -----------------------------------
    A handful of operator-tunable knobs Lyra's Python class exposes
    are NOT addressable on the DLL's public surface:

    * ``density`` — DLL uses its internal default (R=1024).  Memory
      footprint slightly higher than Python's R=64 default but a
      one-time alloc, not a hot-path concern.
    * ``fc_high`` / ``fc_low`` / ``gain`` — DLL uses defaults
      (auto cutoff, no low cutoff, unity gain) which match what
      audio_sink.py actually uses today.
    * ``initial_fill_fraction`` — DLL hardcodes 50%.  Lyra Python's
      default of 80% gave a slightly longer startup-silence period
      (~320 ms vs 200 ms at 400 ms ring), traded for extra consumer
      headroom during the first few seconds.  Native path reverts
      to 200 ms — operator-perceptible at sink open but minor.
    * ``startup_delay`` — DLL hardcodes 3.0 s (vs Lyra Python's 0.5).
      Means the PI control loop kicks in later, so var stays at the
      initial value (typically the persisted last-good var) for the
      first few seconds.  In practice the persisted-var optimization
      means var IS already near steady state at startup, so the
      longer delay is invisible to operators.
    * ``tdelayup`` / ``tdelaydown`` / ``tslewup`` / ``tslewdown``
      flags — DLL has its own slew-time defaults (3 ms via
      ``setRMatchSlewTime``).

    The ones we DO map through to the DLL: ringsize, ff_alpha,
    initial_var (via ``create_rmatchV(...,var)``), prop ringmin/max,
    ff ringmin/max.
    """

    # Same operating range as :class:`RMatch`.  WDSP clamps internally
    # to [0.96, 1.04] regardless of what we say here; we expose the
    # constants for callers that want to validate before passing.
    VAR_MIN: float = 0.96
    VAR_MAX: float = 1.04

    def __init__(
        self,
        insize: int,
        outsize: int,
        nom_inrate: int,
        nom_outrate: int,
        *,
        ringsize: int = 0,
        density: int = 256,                 # accepted, not propagated
        startup_delay: float = 0.5,         # accepted, not propagated
        ff_ringmin: int = 32,
        ff_ringmax: int = 1024,
        ff_alpha: float = 0.05,
        prop_ringmin: int = 32,
        prop_ringmax: int = 4096,
        prop_gain: float = -1.0,
        tslew: float = 0.003,
        fc_high: float = 0.0,               # accepted, not propagated
        fc_low: float = -1.0,               # accepted, not propagated
        gain: float = 1.0,                  # accepted, not propagated
        varmode: int = 1,                   # accepted, DLL hardcodes 1
        initial_var: float = 1.0,
        initial_fill_fraction: float = 0.5, # accepted, not propagated
    ) -> None:
        from lyra.dsp import wdsp_native

        self._lib = wdsp_native.load()
        self._ffi = wdsp_native.ffi()

        self.insize = int(insize)
        self.outsize = int(outsize)
        self.nom_inrate = int(nom_inrate)
        self.nom_outrate = int(nom_outrate)
        self.nom_ratio = float(nom_outrate) / float(nom_inrate)
        self.inv_nom_ratio = float(nom_inrate) / float(nom_outrate)

        # Mirror the Python class's auto-sized ring formula so the DLL
        # gets the same headroom Lyra's Python path used.  WDSP clamps
        # ringsize internally to >= max(2*max_ring_insize, 2*outsize).
        max_ring_insize = int(1.0 + float(insize) * (1.05 * self.nom_ratio))
        if ringsize <= 0:
            ringsize = 2 * max_ring_insize
        if ringsize < 2 * outsize:
            ringsize = 2 * outsize
        self.ringsize = int(ringsize)
        self.rsize = self.ringsize  # alias matches RMatch

        init_var = max(self.VAR_MIN, min(self.VAR_MAX, float(initial_var)))

        # Construct via the convenience constructor — defaults for
        # density / startup / etc. land here; we override the few
        # knobs we expose immediately afterward.
        self._h = self._lib.create_rmatchV(
            self.insize,
            self.outsize,
            self.nom_inrate,
            self.nom_outrate,
            self.ringsize,
            init_var,
        )
        if not self._h:
            raise RuntimeError("create_rmatchV returned NULL")

        # Apply the operator-tunable knobs Lyra cares about.
        # ff/prop ringmax must be powers of two (DLL doesn't validate
        # but the moving-average ring uses a bit mask).
        if ff_ringmax & (ff_ringmax - 1):
            raise ValueError(f"ff_ringmax must be a power of two; got {ff_ringmax}")
        if prop_ringmax & (prop_ringmax - 1):
            raise ValueError(f"prop_ringmax must be a power of two; got {prop_ringmax}")
        self._lib.setRMatchFFRingMin(self._h, int(ff_ringmin))
        self._lib.setRMatchFFRingMax(self._h, int(ff_ringmax))
        self._lib.setRMatchFFAlpha(self._h, float(ff_alpha))
        self._lib.setRMatchPropRingMin(self._h, int(prop_ringmin))
        self._lib.setRMatchPropRingMax(self._h, int(prop_ringmax))
        # WDSP applies pr_gain = prop_gain * 48000 / nom_outrate.
        # If caller asks for the auto-scale (negative sentinel), use
        # the same 0.04/rsize formula RMatch does: max prop term
        # = 0.02 (half the [0.96..1.04] clamp range).
        if prop_gain < 0.0:
            target_pr_gain = 0.04 / float(self.rsize)
            feedback = target_pr_gain * float(self.nom_outrate) / 48000.0
        else:
            feedback = float(prop_gain)
        self._lib.setRMatchFeedbackGain(self._h, feedback)
        self._lib.setRMatchSlewTime(self._h, float(tslew))

        # Pre-allocate the I/O buffers (interleaved I/Q doubles).  The
        # DLL writes/reads exactly insize*2 doubles per xrmatchIN call
        # and outsize*2 doubles per xrmatchOUT call.
        self._in_buf = self._ffi.new(f"double[{2 * self.insize}]")
        self._out_buf = self._ffi.new(f"double[{2 * self.outsize}]")
        self._in_view = np.frombuffer(
            self._ffi.buffer(self._in_buf), dtype=np.float64
        )
        self._out_view = np.frombuffer(
            self._ffi.buffer(self._out_buf), dtype=np.float64
        )

        # Diagnostic readback scratch (allocated once, reused).
        self._diag_uf = self._ffi.new("int*")
        self._diag_of = self._ffi.new("int*")
        self._diag_var = self._ffi.new("double*")
        self._diag_rsize = self._ffi.new("int*")
        self._diag_nring = self._ffi.new("int*")
        self._cf_buf = self._ffi.new("int*")

        # Reservoirs for variable-length read() / read_complex().
        # PortAudio (the typical caller) asks for whatever block size
        # WASAPI/MME negotiates with the device — often 256 or 480 or
        # something else, NOT necessarily our configured ``outsize``.
        # xrmatchOUT pulls EXACTLY ``outsize`` interleaved-IQ doubles
        # per call, so we may need to call it multiple times to fill
        # the operator's request and stash the leftover for next time.
        self._mono_reservoir = np.empty(0, dtype=np.float32)
        self._complex_reservoir = np.empty(0, dtype=np.complex128)

        # Mirror reservoir on the input side.  Lyra's audio worker
        # delivers audio in blocks whose natural size (one process_block
        # ≈ 256 mono samples) doesn't necessarily match our configured
        # ``insize``.  xrmatchIN pulls EXACTLY ``insize`` interleaved-IQ
        # doubles per call, so write() accumulates into a Python-side
        # buffer and drains in whole insize chunks.  Pure-Python
        # ``RMatch`` got this for free via ``varsamp.process`` accepting
        # variable input sizes; the native DLL doesn't, so we mirror
        # the contract here.  Without this, a small write padded with
        # zeros to fill the C buffer produced the classic 1/N-duty-
        # cycle "woodpecker" sound.
        self._in_reservoir_real = np.empty(0, dtype=np.float64)
        self._in_reservoir_imag = np.empty(0, dtype=np.float64)

        self._closed = False

    # ── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        """Free the DLL resources.  Idempotent."""
        if self._closed:
            return
        try:
            self._lib.destroy_rmatchV(self._h)
        finally:
            self._closed = True

    def __del__(self) -> None:  # pragma: no cover (best-effort)
        try:
            self.close()
        except Exception:
            pass

    # ── Producer / consumer hot path ─────────────────────────────

    def write(self, audio_in: np.ndarray) -> int:
        """Push variable-length audio into the rate matcher.

        Accepts mono (real) or complex IQ of ANY length; we accumulate
        into the input reservoir and drain in whole ``insize`` chunks
        via ``xrmatchIN``.  The remainder carries forward — exactly
        the contract pure-Python :class:`RMatch` exposes (its
        underlying ``varsamp.process`` natively accepts variable input).

        Returns
        -------
        int
            Approximate number of samples deposited downstream this
            call (``audio_in.size * var * nom_ratio``).  Caller in
            ``audio_sink`` ignores the return value.
        """
        if self._closed or audio_in.size == 0:
            return 0

        # Append to the reservoir (split into real/imag).
        if np.iscomplexobj(audio_in):
            self._in_reservoir_real = np.concatenate(
                [self._in_reservoir_real,
                 audio_in.real.astype(np.float64, copy=False)]
            )
            self._in_reservoir_imag = np.concatenate(
                [self._in_reservoir_imag,
                 audio_in.imag.astype(np.float64, copy=False)]
            )
        else:
            real = audio_in.astype(np.float64, copy=False)
            self._in_reservoir_real = np.concatenate(
                [self._in_reservoir_real, real]
            )
            # Pad imag with zeros so the two reservoirs stay length-aligned.
            self._in_reservoir_imag = np.concatenate(
                [self._in_reservoir_imag,
                 np.zeros(real.size, dtype=np.float64)]
            )

        # Drain whole insize blocks through xrmatchIN.
        n = self.insize
        consumed = 0
        while self._in_reservoir_real.size >= n:
            self._in_view[0::2] = self._in_reservoir_real[:n]
            self._in_view[1::2] = self._in_reservoir_imag[:n]
            self._lib.xrmatchIN(self._h, self._in_buf)
            self._in_reservoir_real = self._in_reservoir_real[n:]
            self._in_reservoir_imag = self._in_reservoir_imag[n:]
            consumed += n

        return int(round(consumed * self.var * self.nom_ratio))

    def read(self, outsize: Optional[int] = None) -> np.ndarray:
        """Pull ``outsize`` samples (or ``self.outsize`` if None) —
        returns float32 mono.

        Variable-length reads are supported by reservoir-buffering
        between the DLL's fixed-block xrmatchOUT pulls and the
        caller's request.  E.g. caller asks for 480 with a configured
        outsize=256: we call xrmatchOUT twice (512 samples), return
        the first 480, save the remaining 32 for next call.
        """
        n = self.outsize if outsize is None else int(outsize)
        if self._closed or n <= 0:
            return np.zeros(max(0, n), dtype=np.float32)
        # Pull whole xrmatchOUT blocks until the reservoir has enough.
        while self._mono_reservoir.size < n:
            self._lib.xrmatchOUT(self._h, self._out_buf)
            chunk = self._out_view[0::2].astype(np.float32, copy=True)
            if self._mono_reservoir.size == 0:
                self._mono_reservoir = chunk
            else:
                self._mono_reservoir = np.concatenate(
                    [self._mono_reservoir, chunk]
                )
        out = self._mono_reservoir[:n].copy()
        self._mono_reservoir = self._mono_reservoir[n:]
        return out

    def read_complex(self, outsize: Optional[int] = None) -> np.ndarray:
        """Pull ``outsize`` samples — returns complex128.

        Used by IQ-rate-match callers (TX path + future RX2 work)
        that fed complex IQ in.  Same reservoir-buffered variable-
        length contract as :meth:`read`.
        """
        n = self.outsize if outsize is None else int(outsize)
        if self._closed or n <= 0:
            return np.zeros(max(0, n), dtype=np.complex128)
        while self._complex_reservoir.size < n:
            self._lib.xrmatchOUT(self._h, self._out_buf)
            block = np.empty(self.outsize, dtype=np.complex128)
            block.real = self._out_view[0::2]
            block.imag = self._out_view[1::2]
            if self._complex_reservoir.size == 0:
                self._complex_reservoir = block
            else:
                self._complex_reservoir = np.concatenate(
                    [self._complex_reservoir, block]
                )
        out = self._complex_reservoir[:n].copy()
        self._complex_reservoir = self._complex_reservoir[n:]
        return out

    # ── Diagnostics & control ────────────────────────────────────

    def _refresh_diags(self) -> None:
        if self._closed:
            return
        self._lib.getRMatchDiags(
            self._h, self._diag_uf, self._diag_of,
            self._diag_var, self._diag_rsize, self._diag_nring,
        )
        self._lib.getControlFlag(self._h, self._cf_buf)

    @property
    def var(self) -> float:
        self._refresh_diags()
        return float(self._diag_var[0])

    @property
    def underflows(self) -> int:
        self._refresh_diags()
        return int(self._diag_uf[0])

    @property
    def overflows(self) -> int:
        self._refresh_diags()
        return int(self._diag_of[0])

    @property
    def n_ring(self) -> int:
        self._refresh_diags()
        return int(self._diag_nring[0])

    @property
    def control_flag(self) -> bool:
        self._refresh_diags()
        return bool(self._cf_buf[0])

    # The DLL doesn't expose feed_forward / av_deviation — they're
    # internal control-loop state, not observable through the public
    # surface.  Return last-known stand-ins so callers that read these
    # for diagnostics get sensible values rather than AttributeError.
    @property
    def feed_forward(self) -> float:
        return self.var  # at steady state ff ≈ var

    @property
    def av_deviation(self) -> float:
        return 0.0

    def diagnostics(self) -> dict:
        """Return current control-loop state — same shape as
        :meth:`RMatch.diagnostics`."""
        self._refresh_diags()
        return {
            "underflows": int(self._diag_uf[0]),
            "overflows": int(self._diag_of[0]),
            "var": float(self._diag_var[0]),
            "ringsize": int(self._diag_rsize[0]),
            "n_ring": int(self._diag_nring[0]),
            "feed_forward": float(self._diag_var[0]),
            "av_deviation": 0.0,
            "control_active": bool(self._cf_buf[0]),
        }

    def force_var(self, var: float) -> None:
        """Pin the rate multiplier to ``var`` and bypass the control
        loop (diagnostic / bench-test only)."""
        v = float(max(self.VAR_MIN, min(self.VAR_MAX, var)))
        self._lib.forceRMatchVar(self._h, 1, v)

    def unforce_var(self) -> None:
        """Resume normal control-loop operation after ``force_var``."""
        # forceRMatchVar with force=0 disables the override; the
        # ``fvar`` value is irrelevant in that case.
        self._lib.forceRMatchVar(self._h, 0, 1.0)

    def reset(self) -> None:
        """Clear streaming state.

        The DLL doesn't export an in-place "flush state" entry point
        for rmatch; ``reset_rmatch`` is internal and not in the
        ``__declspec(dllexport)`` set.  We approximate by zeroing the
        diagnostic counters via ``resetRMatchDiags`` — the underlying
        ring + var settle quickly on resumed traffic via the existing
        feedback loop, which is what an audio-side rate change /
        stream restart cares about most.
        """
        if self._closed:
            return
        self._lib.resetRMatchDiags(self._h)
        # Drop any unconsumed leftover samples so a stream-restart
        # doesn't bleed pre-discontinuity audio across the gap.
        self._mono_reservoir = np.empty(0, dtype=np.float32)
        self._complex_reservoir = np.empty(0, dtype=np.complex128)
        self._in_reservoir_real = np.empty(0, dtype=np.float64)
        self._in_reservoir_imag = np.empty(0, dtype=np.float64)


__all__ = ["RMatch", "WdspRMatch"]
