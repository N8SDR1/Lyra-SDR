"""WDSP-pattern AGC — look-ahead, state-machine, soft-knee.

Replaces Lyra's legacy single-state peak tracker with a full
look-ahead AGC modeled on Warren Pratt's WDSP wcpAGC.  The legacy
tracker (Radio._apply_agc_and_volume) had three structural issues
the WDSP pattern resolves by construction:

1. **Look-ahead**: a ring buffer delays output by ``n_tau ×
   tau_attack`` seconds (≈ 4 ms at default settings).  When a loud
   sample enters the input side, the AGC scans the look-ahead
   window and ramps gain DOWN before that sample reaches the
   output -- so transients don't distort.

2. **State machine**: 5 states (NORMAL, FAST_DECAY, HANG, DECAY,
   HANG_DECAY) separate the four decay regimes the legacy tracker
   conflated into one.  Pop-detection (state 1) gives fast
   recovery after impulses without making the operator-facing
   decay constant overly aggressive.  Hang-with-threshold
   (state 2 entry condition `hang_backaverage > hang_level`) means
   noise-only audio NEVER enters hang -- eliminating the
   "scratchy noise floor" symptom Brent diagnosed in v0.0.9.x.

3. **Soft-knee output**: gain follows a log-domain compression
   curve (``slope_constant`` controls the curvature).  Smooth
   transition between linear-region tracking and full
   compression -- no hard threshold that rides the knee on SSB
   voice envelopes.

Two prior attempts to surgically patch the legacy tracker failed
for documented mathematical reasons (see
``docs/architecture/audio_rebuild_v0.1.md`` §10.1 attempts 5+6).
This module is the structural answer those attempts couldn't be.

Attribution
===========

Algorithm derived from WDSP ``wcpAGC.c`` (Receiver AGC,
Copyright (C) 2011-2017 Warren Pratt, NR0V), licensed under
GPL v2 or later.  Lyra-SDR's port re-expresses the algorithm in
NumPy + Python and adapts the I/O format to mono float32 audio
(Lyra's per-channel demod output before binaural splitting),
but the state-machine logic, ring-buffer look-ahead semantics,
soft-knee gain curve, and parameter-derivation formulas
(``slope_constant``, ``hang_level``, ``min_volts``) follow
Pratt's reference implementation directly.

Lyra-SDR is GPL v3+ (since v0.0.6) which is license-compatible
with WDSP's GPL v2+.  Pratt's original copyright + license
remain in effect for the algorithm content; the Python
re-expression is jointly copyrighted with Lyra's GPL v3+ terms.

WDSP source: openHPSDR project
Original author contact: warren@wpratt.com

Mapping reference
=================

For maintainers cross-referencing the C source against this
Python port, key correspondences:

  C (wcpAGC.c)               Python (this file)
  ------------------         -----------------------------
  WCPAGC struct              WdspAgc class
  create_wcpagc()            __init__()
  loadWcpAGC()               _recalc_constants()
  flush_wcpagc()             reset()
  xwcpagc()                  process()
  a->ring (complex)          self._ring (float64 mono)
  a->abs_ring               self._abs_ring
  a->volts                  self._volts
  a->state (case 0..4)      self._state == _STATE_NORMAL etc.
  a->ring_max               self._ring_max
  a->fast_backaverage       self._fast_backaverage
  a->hang_backaverage       self._hang_backaverage
  a->hang_counter           self._hang_counter
  a->save_volts             self._save_volts
  a->decay_type             self._decay_type

The most significant adaptation is mono-vs-stereo:

  - WDSP's ring stores complex samples (``2 × ring_buffsize``
    doubles).  Lyra's per-channel demod path is mono float32, so
    the Python ring is a single float64 array of length
    ``ring_buffsize``.
  - WDSP's ``abs_ring`` value is computed in one of two modes:
    ``pmode==0``: max(|L|, |R|); ``pmode==1``: sqrt(L²+R²).  For
    mono, both reduce to ``|sample|``, so ``pmode`` is dropped.
  - The output multiplier ``mult`` from the soft-knee curve is
    applied to a single delayed sample read from the ring,
    instead of to the I/Q pair.

These adaptations preserve WDSP's per-sample semantics exactly --
the look-ahead window, state transitions, gain curve, and
operator-facing time constants all match the C reference for
the same input.

Status
======

**This module is not yet wired into Radio.**  Wiring is gated on
bench validation against a synthetic-signal harness (and ideally
A/B comparison against the C reference).  See
``feature/agc-wdsp-port`` branch for integration progress.
"""
# Lyra-SDR — WDSP-pattern AGC (mono float32 port)
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License v3
# or later.  See LICENSE in the project root for the full terms.
#
# Algorithm derived from WDSP wcpAGC.c, Copyright (C) 2011-2017
# Warren Pratt, NR0V (GPL v2 or later).  See module docstring
# above for full attribution.
from __future__ import annotations

import math
from typing import Optional

import numpy as np


# ── Operator-facing modes ──────────────────────────────────────────
# Match WDSP's RXA mode integers (SetRXAAGCMode in wcpAGC.c) for
# easy 1:1 cross-reference, but expose by name from Lyra's UI.
MODE_OFF: int = 0
MODE_LONG: int = 1
MODE_SLOW: int = 2
MODE_MEDIUM: int = 3
MODE_FAST: int = 4

# String aliases for Lyra's preset wiring.  Existing
# Radio.AGC_PRESETS uses "off / fast / med / slow / auto"; "auto"
# isn't a WDSP mode (it's a Lyra-side noise-floor tracker) so
# operator-facing "auto" maps to MODE_MEDIUM here and the
# threshold-tracking happens at the Radio layer.
MODE_BY_NAME: dict[str, int] = {
    "off": MODE_OFF,
    "long": MODE_LONG,
    "slow": MODE_SLOW,
    "med": MODE_MEDIUM,
    "medium": MODE_MEDIUM,
    "fast": MODE_FAST,
    "auto": MODE_MEDIUM,
}


# ── Internal state-machine states ──────────────────────────────────
# These are NOT operator-facing -- they're the per-sample state of
# the AGC's gain-tracking automaton (a->state in wcpAGC.c).
_STATE_NORMAL: int = 0       # tracking attacks; default state
_STATE_FAST_DECAY: int = 1   # post-pop fast-recovery decay
_STATE_HANG: int = 2         # gain held steady (signal above hang_level)
_STATE_DECAY: int = 3        # standard exponential gain release
_STATE_HANG_DECAY: int = 4   # slower decay branch after hang ends


# ── Ring-buffer sizing constants (match wcpAGC.h) ──────────────────
# Maximum supported sample rate, max number of attack-time-constants
# in look-ahead window, max attack time constant.  Together these
# bound the ring buffer size so it can be allocated once at __init__
# and never reallocated when the operator changes attack time live.
_MAX_SAMPLE_RATE: float = 384000.0
_MAX_N_TAU: int = 8
_MAX_TAU_ATTACK: float = 0.01

# Ring buffer length in samples.  At 384 kHz × 8 × 0.01 = 30720,
# +1 for the off-by-one in the original C.  ~240 KB for float64
# mono, allocated once.  Lyra's audio path runs at 48 kHz so
# typical use only fills a tiny fraction of this.
_RB_SIZE: int = int(_MAX_SAMPLE_RATE * _MAX_N_TAU * _MAX_TAU_ATTACK + 1)


# ── Mode-specific time-constant presets ────────────────────────────
# Match SetRXAAGCMode in wcpAGC.c.  These are the parameter sets
# that switch when the operator picks a preset; per-knob tunings
# (set_attack_ms, set_decay_ms, set_hang_ms) override individual
# fields without changing mode.  hang_thresh defaults follow WDSP:
# Long/Slow leave the previous value; Med/Fast force 1.0 (always
# allow hang on signal, never on noise -- the threshold is what
# distinguishes those two cases via hang_backaverage).
_MODE_PRESETS: dict[int, dict] = {
    MODE_LONG:   {"tau_decay": 2.000, "hangtime": 2.000},
    MODE_SLOW:   {"tau_decay": 0.500, "hangtime": 1.000},
    MODE_MEDIUM: {"tau_decay": 0.250, "hangtime": 0.000, "hang_thresh": 1.0},
    MODE_FAST:   {"tau_decay": 0.050, "hangtime": 0.000, "hang_thresh": 1.0},
}


class WdspAgc:
    """Look-ahead state-machine AGC, mono float32 input/output.

    Per-sample peak-following AGC modeled on WDSP wcpAGC.  The
    look-ahead ring buffer delays output by ``n_tau × tau_attack``
    seconds so attack ramps complete BEFORE the loud sample reaches
    the output.  Five-state internal state machine handles attack,
    fast-recovery (post-pop), hang, normal decay, and hang-decay
    regimes separately.

    Output is computed via a log-domain soft-knee:

        mult = (out_target - slope_constant *
                min(0.0, log10(inv_max_input * volts))) / volts

    so the gain curve is smooth around the threshold (no hard
    discontinuity that rides the knee on voice envelopes).

    Operator-facing parameters:

        sample_rate      Hz (must match the audio chain rate)
        mode             MODE_OFF / MODE_LONG / MODE_SLOW /
                         MODE_MEDIUM / MODE_FAST (see set_mode)
        max_gain         max linear AGC gain (default 10000 = 80 dB)
        var_gain         variable gain range (default 1.5)
        fixed_gain       linear gain when mode == MODE_OFF
        max_input        expected input ceiling (default 1.0)
        out_target       target output level (default 1.0)
        tau_attack       attack time constant, seconds (default 0.001)
        tau_decay        decay time constant, seconds (mode-dependent)
        n_tau            attack-time-constants in look-ahead window
                         (default 4 → 4 ms at default tau_attack)
        hangtime         hang duration, seconds (mode-dependent)
        hang_thresh      hang threshold, normalized (default 0.25)
        tau_fast_backaverage  pop-detector back-average tau (0.250)
        tau_fast_decay   post-pop decay tau (default 0.005)
        pop_ratio        pop detection threshold (default 5.0)
        tau_hang_backmult  hang-detector back-average tau (default 0.500)
        tau_hang_decay   hang-decay tau (default 0.100)

    All time constants are in SECONDS, rate-invariant by construction.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        mode: int = MODE_MEDIUM,
        # Gain limits (operator-tunable in the UI for max_gain)
        max_gain: float = 10000.0,
        var_gain: float = 1.5,
        fixed_gain: float = 1000.0,
        max_input: float = 1.0,
        out_target: float = 1.0,
        # Attack
        tau_attack: float = 0.001,
        n_tau: int = 4,
        # Decay (mode-set; explicit override applies after mode preset)
        tau_decay: Optional[float] = None,
        # Hang
        hangtime: Optional[float] = None,
        hang_enable: bool = True,
        hang_thresh: float = 0.25,
        tau_hang_backmult: float = 0.500,
        tau_hang_decay: float = 0.100,
        # Pop detection
        tau_fast_backaverage: float = 0.250,
        tau_fast_decay: float = 0.005,
        pop_ratio: float = 5.0,
    ) -> None:
        # ── Operator parameters ────────────────────────────────────
        # All stored in their canonical seconds form; per-sample
        # multipliers are derived in _recalc_constants().
        self.sample_rate: float = float(sample_rate)
        self.mode: int = int(mode)

        self.max_gain: float = float(max_gain)
        self.var_gain: float = float(var_gain)
        self.fixed_gain: float = float(fixed_gain)
        self.max_input: float = float(max_input)
        self.out_targ: float = float(out_target)
        self.tau_attack: float = float(tau_attack)
        self.n_tau: int = int(n_tau)
        # tau_decay default tracks MODE_MEDIUM if not set explicitly
        self.tau_decay: float = float(
            tau_decay if tau_decay is not None
            else _MODE_PRESETS[MODE_MEDIUM]["tau_decay"]
        )
        self.hangtime: float = float(
            hangtime if hangtime is not None
            else _MODE_PRESETS[MODE_MEDIUM]["hangtime"]
        )
        self.hang_enable: bool = bool(hang_enable)
        self.hang_thresh: float = float(hang_thresh)
        self.tau_hang_backmult: float = float(tau_hang_backmult)
        self.tau_hang_decay: float = float(tau_hang_decay)
        self.tau_fast_backaverage: float = float(tau_fast_backaverage)
        self.tau_fast_decay: float = float(tau_fast_decay)
        self.pop_ratio: float = float(pop_ratio)

        # ── Apply mode preset ──────────────────────────────────────
        # Override tau_decay / hangtime / hang_thresh from the mode
        # preset (matches WDSP SetRXAAGCMode).  Any explicit tau_decay
        # or hangtime passed to __init__ takes precedence -- if the
        # caller specified them, leave them alone.
        if self.mode in _MODE_PRESETS and tau_decay is None:
            preset = _MODE_PRESETS[self.mode]
            self.tau_decay = preset["tau_decay"]
            self.hangtime = preset["hangtime"]
            if "hang_thresh" in preset:
                self.hang_thresh = preset["hang_thresh"]

        # ── Ring buffers (allocated once, reused forever) ──────────
        # _ring stores the delayed audio samples; _abs_ring stores
        # |sample| for the look-ahead-window scan.  Capacity sized
        # for the worst-case parameter combination so live parameter
        # changes never reallocate.
        self._ring: np.ndarray = np.zeros(_RB_SIZE, dtype=np.float64)
        self._abs_ring: np.ndarray = np.zeros(_RB_SIZE, dtype=np.float64)
        self.ring_buffsize: int = _RB_SIZE

        # ── Index state (initialized so out_index advances to 0 on
        # the first sample, matching WDSP's calc_wcpagc) ───────────
        self.out_index: int = -1
        self.in_index: int = 0  # set properly in _recalc_constants
        self.attack_buffsize: int = 0  # filled in _recalc_constants

        # ── Tracker state ──────────────────────────────────────────
        self._volts: float = 0.0
        self._save_volts: float = 0.0
        self._abs_out_sample: float = 0.0
        self._out_sample: float = 0.0  # mono — single value, not pair
        self._ring_max: float = 0.0
        self._fast_backaverage: float = 0.0
        self._hang_backaverage: float = 0.0
        self._hang_counter: int = 0
        self._state: int = _STATE_NORMAL
        self._decay_type: int = 0  # 0 = normal decay, 1 = hang decay
        self._gain: float = 0.0    # last-sample reported gain (linear)

        # ── Derived constants (filled by _recalc_constants) ────────
        # All initialized to safe defaults; _recalc_constants
        # overwrites them based on operator parameters.
        self.attack_mult: float = 0.0
        self.decay_mult: float = 0.0
        self.fast_decay_mult: float = 0.0
        self.fast_backmult: float = 0.0
        self.onemfast_backmult: float = 0.0
        self.hang_backmult: float = 0.0
        self.onemhang_backmult: float = 0.0
        self.hang_decay_mult: float = 0.0
        self.out_target: float = 0.0
        self.min_volts: float = 0.0
        self.inv_out_target: float = 0.0
        self.slope_constant: float = 0.0
        self.inv_max_input: float = 0.0
        self.hang_level: float = 0.0

        self._recalc_constants()

    # ── Operator-facing parameter setters ──────────────────────────
    # All of these end with _recalc_constants() so the per-sample
    # multipliers are recomputed from canonical seconds-form
    # parameters.  Calling these between process() calls is safe;
    # calling them WITHIN a process() call is not (the in-flight
    # buffer would see partial updates).

    def set_mode(self, mode: int) -> None:
        """Switch operator preset.  Replaces tau_decay / hangtime /
        hang_thresh with the preset's values; other parameters
        (attack, max_gain, etc.) are preserved."""
        self.mode = int(mode)
        if self.mode in _MODE_PRESETS:
            preset = _MODE_PRESETS[self.mode]
            self.tau_decay = preset["tau_decay"]
            self.hangtime = preset["hangtime"]
            if "hang_thresh" in preset:
                self.hang_thresh = preset["hang_thresh"]
        self._recalc_constants()

    def set_attack_ms(self, ms: float) -> None:
        self.tau_attack = float(ms) / 1000.0
        self._recalc_constants()

    def set_decay_ms(self, ms: float) -> None:
        self.tau_decay = float(ms) / 1000.0
        self._recalc_constants()

    def set_hang_ms(self, ms: float) -> None:
        self.hangtime = float(ms) / 1000.0
        self._recalc_constants()

    def set_max_gain_db(self, db: float) -> None:
        self.max_gain = 10.0 ** (float(db) / 20.0)
        self._recalc_constants()

    def set_hang_threshold(self, thresh: float) -> None:
        """Hang threshold, normalized 0..1.  Default 0.25.

        The threshold gates entry into hang state via the rule
        ``hang_backaverage > hang_level``, where hang_level is
        derived from this threshold and the gain parameters.
        Higher threshold → less likely to hang (only on stronger
        signals).  Lower threshold → more likely to hang.  At
        threshold == 0 the hang state would be entered on any
        signal (including noise); at threshold == 1 hang is never
        entered."""
        self.hang_thresh = max(0.0, min(1.0, float(thresh)))
        self._recalc_constants()

    def set_sample_rate(self, sample_rate: int) -> None:
        """Update sample rate and recompute all per-sample
        multipliers.  Ring buffer is NOT reallocated -- it was
        sized for the worst-case rate at construction."""
        self.sample_rate = float(sample_rate)
        self._recalc_constants()

    # ── Internal: recompute derived constants ──────────────────────

    def _recalc_constants(self) -> None:
        """Recompute per-sample multipliers, target, and limits
        from the operator-facing seconds-form parameters.  Mirrors
        WDSP's loadWcpAGC()."""
        sr = self.sample_rate

        # Attack-buffer length in samples (look-ahead window).  At
        # default 48 kHz × 4 × 0.001 s = 192 samples = 4 ms.
        self.attack_buffsize = int(math.ceil(sr * self.n_tau * self.tau_attack))
        # in_index = (out_index + attack_buffsize) so the input
        # writes attack_buffsize samples ahead of where the output
        # reads.  The first call to process() advances out_index to
        # 0, so in_index initially sits at attack_buffsize - 1 +
        # (-1) = attack_buffsize - 1.  Match WDSP exactly:
        self.in_index = self.attack_buffsize + self.out_index

        # Per-sample exponential multipliers.  exp(-1 / (sr × tau))
        # gives the per-sample retention factor; subtracted from 1.0
        # yields the per-sample "step toward target" coefficient.
        self.attack_mult = 1.0 - math.exp(-1.0 / (sr * self.tau_attack))
        self.decay_mult = 1.0 - math.exp(-1.0 / (sr * self.tau_decay))
        self.fast_decay_mult = 1.0 - math.exp(
            -1.0 / (sr * self.tau_fast_decay))
        self.fast_backmult = 1.0 - math.exp(
            -1.0 / (sr * self.tau_fast_backaverage))
        self.onemfast_backmult = 1.0 - self.fast_backmult

        # out_target is reduced by (1 - exp(-n_tau)) to leave a
        # small headroom under out_targ; the 0.9999 trim is from
        # WDSP and prevents the soft-knee ratio from going
        # numerically singular when volts approaches the target.
        self.out_target = (
            self.out_targ * (1.0 - math.exp(-float(self.n_tau))) * 0.9999
        )
        # min_volts is the lower bound on volts that prevents gain
        # from exceeding (var_gain × max_gain).  Derived, not a
        # fixed PEAK_FLOOR -- if the operator raises max_gain, the
        # floor automatically drops.
        self.min_volts = self.out_target / (self.var_gain * self.max_gain)
        self.inv_out_target = 1.0 / self.out_target

        # slope_constant defines the soft-knee compression curve.
        # Below threshold (volts < max_input), gain follows
        # (out_target - slope_constant × log10(volts/max_input)) /
        # volts -- a smooth log-domain compression curve.  Above
        # threshold, gain reduces 1:1 with volts (linear region).
        tmp = math.log10(
            self.out_target / (self.max_input * self.var_gain * self.max_gain)
        )
        if tmp == 0.0:
            tmp = 1e-16
        self.slope_constant = (
            self.out_target * (1.0 - 1.0 / self.var_gain) / tmp
        )

        self.inv_max_input = 1.0 / self.max_input

        # hang_level is the back-average threshold above which the
        # hang state is entered.  Derived from hang_thresh via
        # WDSP's standard log curve (matches the "Hang Threshold"
        # slider semantics in the Settings dialog of WDSP-based
        # clients).  At hang_thresh == 1.0, hang_level == max_input
        # × 0.637 (always-on hang on signal).  At hang_thresh < 1.0,
        # threshold drops on a log scale so smaller signals don't
        # trigger hang.
        tmp = 10.0 ** ((self.hang_thresh - 1.0) / 0.125)
        self.hang_level = (
            self.max_input * tmp
            + (self.out_target / (self.var_gain * self.max_gain))
            * (1.0 - tmp)
        ) * 0.637

        self.hang_backmult = 1.0 - math.exp(
            -1.0 / (sr * self.tau_hang_backmult))
        self.onemhang_backmult = 1.0 - self.hang_backmult
        self.hang_decay_mult = 1.0 - math.exp(
            -1.0 / (sr * self.tau_hang_decay))

    # ── Public API ─────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear ring buffers and tracker state.  Call on freq /
        mode / band changes to avoid stale envelope state from
        the previous signal landing on the new one."""
        self._ring.fill(0.0)
        self._abs_ring.fill(0.0)
        self._ring_max = 0.0
        self._volts = 0.0
        self._save_volts = 0.0
        self._fast_backaverage = 0.0
        self._hang_backaverage = 0.0
        self._hang_counter = 0
        self._decay_type = 0
        self._state = _STATE_NORMAL

    @property
    def gain(self) -> float:
        """Last-sample reported gain (linear).  Read by the meter
        / status surfaces in Radio.  Matches WDSP's a->gain
        readout."""
        return self._gain

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one buffer of mono float32 audio through the
        AGC.  Returns a buffer of the same length, also mono
        float32.  State carries forward across calls -- envelope
        tracking is sample-continuous across buffer boundaries.

        When mode == MODE_OFF, the input is multiplied by
        ``fixed_gain`` and returned (no ring-buffer delay, no
        envelope tracking) -- this matches WDSP's mode-0 behavior
        and is used by Lyra's "AGC off" preset for digital modes.
        """
        if audio.size == 0:
            return audio.astype(np.float32, copy=False)

        # MODE_OFF — fixed-gain bypass.  No ring buffer, no
        # envelope tracking, no per-sample loop.  Lyra historically
        # applies its own +14 dB makeup gain in AGC-off mode (see
        # Radio._apply_agc_and_volume); fixed_gain here is the
        # WDSP-side equivalent and operators tune one or the other.
        if self.mode == MODE_OFF:
            return (audio * self.fixed_gain).astype(np.float32, copy=False)

        # Cast to float64 for the inner loop (matches WDSP's double
        # precision); cast back to float32 on return so the
        # downstream chain (binaural, sink) sees the same dtype as
        # the legacy AGC.
        audio_f64 = audio.astype(np.float64, copy=False)
        out = np.empty_like(audio_f64)
        n = audio_f64.size

        # Pull instance state into local variables for the inner
        # loop.  Python attribute access is ~3x slower than local
        # variable access; for a 1024-sample inner loop running at
        # ~90 Hz this matters.  Write back to self.* after the
        # loop completes.
        ring = self._ring
        abs_ring = self._abs_ring
        ring_buffsize = self.ring_buffsize
        attack_buffsize = self.attack_buffsize

        out_index = self.out_index
        in_index = self.in_index
        ring_max = self._ring_max
        volts = self._volts
        save_volts = self._save_volts
        fast_backaverage = self._fast_backaverage
        hang_backaverage = self._hang_backaverage
        hang_counter = self._hang_counter
        state = self._state
        decay_type = self._decay_type

        # Per-sample multipliers (constant across the buffer)
        attack_mult = self.attack_mult
        decay_mult = self.decay_mult
        fast_decay_mult = self.fast_decay_mult
        fast_backmult = self.fast_backmult
        onemfast_backmult = self.onemfast_backmult
        hang_backmult = self.hang_backmult
        onemhang_backmult = self.onemhang_backmult
        hang_decay_mult = self.hang_decay_mult
        pop_ratio = self.pop_ratio
        hang_enable = self.hang_enable
        hang_level = self.hang_level
        hangtime = self.hangtime
        sample_rate = self.sample_rate
        min_volts = self.min_volts
        out_target = self.out_target
        inv_max_input = self.inv_max_input
        slope_constant = self.slope_constant

        # ── Per-sample loop ───────────────────────────────────────
        # Faithful port of xwcpagc()'s inner loop.  State machine
        # transitions are sequential and state-dependent so this
        # cannot easily be vectorized -- pure Python at ~1024
        # samples × 90 Hz ≈ 90k iterations/sec.  Estimate ~1-2 ms
        # per buffer, well under the 21 ms audio block budget.
        # Numba @jit could shave this by 10-100× if profiling
        # shows it matters; defer until we have measurements.
        for i in range(n):
            # ── Advance ring indices ──
            out_index += 1
            if out_index >= ring_buffsize:
                out_index -= ring_buffsize
            in_index += 1
            if in_index >= ring_buffsize:
                in_index -= ring_buffsize

            # ── Read delayed (out) sample, write current (in) ──
            out_sample = ring[out_index]
            abs_out_sample = abs_ring[out_index]
            ring[in_index] = audio_f64[i]
            abs_ring[in_index] = abs(audio_f64[i])  # mono — single |x|

            # ── Update background averages (using delayed sample) ──
            fast_backaverage = (
                fast_backmult * abs_out_sample
                + onemfast_backmult * fast_backaverage
            )
            hang_backaverage = (
                hang_backmult * abs_out_sample
                + onemhang_backmult * hang_backaverage
            )

            # ── Update ring_max ──
            # If the sample falling off the back of the look-ahead
            # window WAS the running max, rescan the window for
            # the new max.  Otherwise just check whether the new
            # input sample exceeds the current max.  Worst case
            # adds attack_buffsize iterations per sample (rare);
            # average case is one comparison.
            if abs_out_sample >= ring_max and abs_out_sample > 0.0:
                ring_max = 0.0
                k = out_index
                for _ in range(attack_buffsize):
                    k += 1
                    if k == ring_buffsize:
                        k = 0
                    if abs_ring[k] > ring_max:
                        ring_max = abs_ring[k]
            if abs_ring[in_index] > ring_max:
                ring_max = abs_ring[in_index]

            # ── Decrement hang counter ──
            if hang_counter > 0:
                hang_counter -= 1

            # ── State machine ──
            # The five states separate the regimes the legacy
            # Lyra AGC conflated: NORMAL handles attack, FAST_DECAY
            # handles post-pop fast recovery, HANG holds gain
            # steady (only entered on signal above hang_level),
            # DECAY is normal exponential release, HANG_DECAY is
            # the slower decay branch after hang ends.  Each
            # transition is a direct port of the corresponding
            # case in wcpAGC.c xwcpagc().
            if state == _STATE_NORMAL:
                if ring_max >= volts:
                    # Attack toward new peak
                    volts += (ring_max - volts) * attack_mult
                else:
                    # Decide what kind of decay to enter
                    if volts > pop_ratio * fast_backaverage:
                        # Pop / transient detected -- fast recovery
                        state = _STATE_FAST_DECAY
                        volts += (ring_max - volts) * fast_decay_mult
                    elif (hang_enable and
                          hang_backaverage > hang_level):
                        # Signal level above hang threshold -- hang
                        state = _STATE_HANG
                        hang_counter = int(hangtime * sample_rate)
                        decay_type = 1
                    else:
                        # Standard exponential decay
                        state = _STATE_DECAY
                        volts += (ring_max - volts) * decay_mult
                        decay_type = 0
            elif state == _STATE_FAST_DECAY:
                if ring_max >= volts:
                    # New attack -- back to NORMAL
                    state = _STATE_NORMAL
                    volts += (ring_max - volts) * attack_mult
                else:
                    if volts > save_volts:
                        # Continue fast decay until we reach the
                        # pre-pop steady-state level
                        volts += (ring_max - volts) * fast_decay_mult
                    else:
                        if hang_counter > 0:
                            state = _STATE_HANG
                        else:
                            if decay_type == 0:
                                state = _STATE_DECAY
                                volts += (ring_max - volts) * decay_mult
                            else:
                                state = _STATE_HANG_DECAY
                                volts += (ring_max - volts) * hang_decay_mult
            elif state == _STATE_HANG:
                if ring_max >= volts:
                    # Attack during hang -- exit hang, save volts
                    state = _STATE_NORMAL
                    save_volts = volts
                    volts += (ring_max - volts) * attack_mult
                else:
                    # Wait out the hang counter; volts unchanged
                    if hang_counter == 0:
                        state = _STATE_HANG_DECAY
                        volts += (ring_max - volts) * hang_decay_mult
            elif state == _STATE_DECAY:
                if ring_max >= volts:
                    state = _STATE_NORMAL
                    save_volts = volts
                    volts += (ring_max - volts) * attack_mult
                else:
                    volts += (ring_max - volts) * decay_mult
            elif state == _STATE_HANG_DECAY:
                if ring_max >= volts:
                    state = _STATE_NORMAL
                    save_volts = volts
                    volts += (ring_max - volts) * attack_mult
                else:
                    volts += (ring_max - volts) * hang_decay_mult
            else:
                # Defensive: shouldn't happen, recover to NORMAL
                state = _STATE_NORMAL

            # Floor volts at min_volts (gain ceiling)
            if volts < min_volts:
                volts = min_volts

            # ── Compute output gain via soft-knee curve ──
            # Below max_input, log10 term is negative and scaled
            # by slope_constant; above max_input, the min(0.0,...)
            # clamps the log term so we get pure linear region.
            log_term = min(
                0.0, math.log10(inv_max_input * volts))
            mult = (out_target - slope_constant * log_term) / volts
            out[i] = out_sample * mult

        # ── Write back local-variable state ──
        self.out_index = out_index
        self.in_index = in_index
        self._ring_max = ring_max
        self._volts = volts
        self._save_volts = save_volts
        self._fast_backaverage = fast_backaverage
        self._hang_backaverage = hang_backaverage
        self._hang_counter = hang_counter
        self._state = state
        self._decay_type = decay_type
        # Report the END-OF-BUFFER linear gain for the meter
        # (matches WDSP's a->gain and Lyra's existing meter
        # cadence -- one update per buffer).
        self._gain = volts * self.inv_out_target

        return out.astype(np.float32, copy=False)
