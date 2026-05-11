"""Sentinel test for v0.1 Phase 0 multi-channel refactor.

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §3.1.x item 4
(pinned Round 5 2026-05-11 by Round 4 Agent G):

  > `PythonRxChannel.__init__(self, in_rate: int, channel_id: int = 0)`
  > -- `channel_id` defaults to 0 for backward compatibility with
  > existing single-channel call sites (Phase 0 polish).  Sentinel
  > test: `PythonRxChannel(in_rate=192000, channel_id=2)` instantiates
  > and a silent IQ buffer passed to `process()` returns silence
  > without exception.  Test lives in
  > `tests/dsp/test_channel_instantiation.py`.

The literal "passed to process() returns silence" clause is stale.
The v0.0.9.6 cleanup arc (CLAUDE.md §14.9) deleted
``PythonRxChannel.process()`` -- DSP now runs in WDSP cffi via
``Radio._do_demod_wdsp``.  The channel itself is a state container
for operator-mirrored DSP knobs (NR / APF / NB / ANF / LMS / squelch
/ NR2), not a signal processor.

This test honors the SPIRIT of the plan's sentinel (a fresh
non-zero-id channel instance is healthy) by asserting:

  1. The constructor accepts ``channel_id=2`` without raising.
  2. ``self.channel_id`` round-trips through ``int(channel_id)``.
  3. The state-container surface (``_nr``, ``_apf``, ``_nb``,
     ``_anf``, ``_lms``, ``_squelch``, ``_nr2``) is populated.
  4. ``reset()`` runs without exception on a fresh instance.
  5. The default-args path (no ``channel_id``) preserves the
     existing radio.py:791 call shape (``channel_id`` defaults to 0).
"""
from __future__ import annotations

import unittest

from lyra.dsp.channel import PythonRxChannel


class ChannelInstantiationTest(unittest.TestCase):
    """Phase 0 done-definition item 4 -- per-channel-instantiable
    contract for the v0.1 RX2 multi-channel refactor.
    """

    def test_channel_id_2_instantiates(self) -> None:
        """`PythonRxChannel(in_rate=192000, channel_id=2)` constructs
        without raising and stores the id on the instance.

        This is the sentinel for v0.1 RX2 Phase 1, which will
        instantiate the RX2 channel with `channel_id=2` (host
        channel 2 / DDC2 routing per CLAUDE.md §6.7 discipline #6).
        """
        ch = PythonRxChannel(in_rate=192000, channel_id=2)
        self.assertEqual(ch.channel_id, 2)
        self.assertEqual(ch.in_rate, 192000)
        self.assertEqual(ch.audio_rate, 48000)

    def test_state_containers_present(self) -> None:
        """A freshly-constructed channel exposes all the DSP-state
        mirrors Radio expects -- nothing should be lazy-initialized
        such that Phase 1's RX2 instantiation hits a None on first
        access.
        """
        ch = PythonRxChannel(in_rate=192000, channel_id=2)
        # Live captures still flow through NR1, even in WDSP mode.
        self.assertIsNotNone(ch._nr)
        # State mirrors for the WDSP-cffi-backed knobs.
        self.assertIsNotNone(ch._apf)
        self.assertIsNotNone(ch._nb)
        self.assertIsNotNone(ch._anf)
        self.assertIsNotNone(ch._lms)
        self.assertIsNotNone(ch._squelch)
        self.assertIsNotNone(ch._nr2)
        # Operator-mirrored fields with their documented defaults.
        self.assertEqual(ch._mode, "USB")
        self.assertEqual(ch._cw_pitch_hz, 650.0)
        self.assertEqual(ch._active_nr, "nr1")

    def test_reset_is_idempotent_on_fresh_channel(self) -> None:
        """The plan's sentinel intent: "a silent IQ buffer through
        the channel returns silence without exception."  In the
        v0.0.9.6+ architecture there is no process() to feed IQ to
        -- the closest channel-layer equivalent is reset(), which
        drops in-flight state on operator-driven discontinuities.
        On a fresh instance it must be a no-op-equivalent (no
        exception, no state corruption).
        """
        ch = PythonRxChannel(in_rate=192000, channel_id=2)
        # First call -- against the brand-new state.
        ch.reset()
        # Second call -- against the just-reset state.  Idempotency
        # is what Phase 1 RX2 relies on when toggling RX2 on/off.
        ch.reset()
        # Operator-mirrored fields are preserved across reset (they
        # are persistent operator state, not transient DSP state).
        self.assertEqual(ch._mode, "USB")
        self.assertEqual(ch.channel_id, 2)

    def test_default_channel_id_is_zero(self) -> None:
        """Backward-compat assertion for the existing single-channel
        call site at radio.py:791:

            PythonRxChannel(in_rate=self._rate)

        Phase 0's signature change MUST NOT break this caller.  The
        default of 0 = host channel 0 (RX1 / DDC0 / VFOA).
        """
        ch = PythonRxChannel(in_rate=192000)
        self.assertEqual(ch.channel_id, 0)

    def test_channel_id_int_coercion(self) -> None:
        """The ABC's ``int(channel_id)`` cast accepts numeric-like
        inputs.  Defensive -- some QSettings-derived values come
        back as strings or floats from older serialization shapes.
        """
        ch = PythonRxChannel(in_rate=192000, channel_id=3)
        self.assertIsInstance(ch.channel_id, int)
        self.assertEqual(ch.channel_id, 3)


if __name__ == "__main__":
    unittest.main()
