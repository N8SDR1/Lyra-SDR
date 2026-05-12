"""Phase 3.A v0.1 — per-RX state + focused-RX foundation tests.

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §6.1 + §6.7:
the hybrid focus model needs per-RX state fields on ``Radio`` that
the UI binds to.  Phase 3.A introduces the fields, the
``_focused_rx`` axis, and the ``set_focused_rx`` / ``focused_rx``
surface; Phase 3.B+ wires UI consumers + introduces ``target_rx``
semantics on setters that let RX2 state diverge from RX1's.

This test module verifies the Phase 3.A invariants:

* Per-RX state fields exist on a freshly-constructed ``Radio``.
* They are initialized identically to the corresponding RX1
  state (lock-step invariant during the Phase 3.A transition).
* ``_focused_rx`` defaults to 0 (RX1) and ``focused_rx`` reads
  back as 0.
* ``set_focused_rx(2)`` transitions focus to RX2 and emits
  ``focused_rx_changed(2)``.
* ``set_focused_rx`` is idempotent (no emit on a no-op).
* ``set_focused_rx`` validates input (raises on unknown rx_id).
* ``_resolve_rx_target`` maps ``None / 0 / 2`` correctly.

Run from repo root::

    python -m unittest tests.ui.test_focused_rx_state -v
"""
from __future__ import annotations

import sys
import unittest

from PySide6.QtCore import QObject


class FocusedRxStateTest(unittest.TestCase):
    """Phase 3.A v0.1 — focus + per-RX state foundation."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    # ── Per-RX state field existence + lock-step at init ────────────

    def test_per_rx_state_fields_exist(self) -> None:
        """All five Phase 3.A per-RX state fields are present on
        the Radio instance."""
        for name in (
            "_mode_rx2",
            "_rx_bw_by_mode_rx2",
            "_agc_profile_rx2",
            "_agc_target_rx2",
            "_af_gain_db_rx2",
        ):
            with self.subTest(field=name):
                self.assertTrue(
                    hasattr(self.radio, name),
                    f"Radio missing Phase 3.A per-RX field {name!r}",
                )

    def test_per_rx_state_initial_lockstep_with_rx1(self) -> None:
        """At construction time the per-RX state must equal the
        RX1 state (the Phase 3.A invariant: ``_<base>_rx2 ==
        _<base>``).  Phase 3.B introduces divergence."""
        r = self.radio
        self.assertEqual(r._mode_rx2, r._mode)
        self.assertEqual(r._rx_bw_by_mode_rx2, r._rx_bw_by_mode)
        # Defensive: the two should be DISTINCT dict objects (one is
        # a copy of the other, so future mutations to one don't
        # leak into the other once Phase 3.B starts diverging).
        self.assertIsNot(r._rx_bw_by_mode_rx2, r._rx_bw_by_mode)
        self.assertEqual(r._agc_profile_rx2, r._agc_profile)
        self.assertEqual(r._agc_target_rx2, r._agc_target)
        self.assertEqual(r._af_gain_db_rx2, r._af_gain_db)

    # ── Focus state field + property ────────────────────────────────

    def test_focused_rx_defaults_to_rx1(self) -> None:
        """Fresh Radio defaults focus to RX1 (channel ID 0).
        Phase 3.B+ persistence may restore the operator's last
        focused RX; Phase 3.A defaults to 0."""
        self.assertEqual(self.radio.focused_rx, 0)
        self.assertEqual(self.radio._focused_rx, 0)

    def test_set_focused_rx_to_rx2_emits_signal(self) -> None:
        received: list[int] = []
        self.radio.focused_rx_changed.connect(received.append)
        self.radio.set_focused_rx(2)
        self.assertEqual(self.radio.focused_rx, 2)
        self.assertEqual(received, [2])

    def test_set_focused_rx_back_to_rx1_emits_transition(self) -> None:
        received: list[int] = []
        self.radio.focused_rx_changed.connect(received.append)
        self.radio.set_focused_rx(2)
        self.radio.set_focused_rx(0)
        self.assertEqual(self.radio.focused_rx, 0)
        self.assertEqual(received, [2, 0])

    def test_set_focused_rx_is_idempotent(self) -> None:
        """Setting focus to current value MUST NOT emit
        ``focused_rx_changed`` (avoids unnecessary UI re-bind work
        + signal storms)."""
        received: list[int] = []
        self.radio.focused_rx_changed.connect(received.append)
        # Already at 0; setting to 0 is a no-op.
        self.radio.set_focused_rx(0)
        self.assertEqual(received, [])
        # Move to 2, then 2 again — second is no-op.
        self.radio.set_focused_rx(2)
        self.radio.set_focused_rx(2)
        self.assertEqual(received, [2])

    def test_set_focused_rx_validates_input(self) -> None:
        for bad in (1, 3, -1, 99):
            with self.subTest(rx_id=bad):
                with self.assertRaises(ValueError):
                    self.radio.set_focused_rx(bad)

    # ── _resolve_rx_target helper ───────────────────────────────────

    def test_resolve_rx_target_none_uses_focused(self) -> None:
        # Default focus is RX1.
        rx_id, suffix = self.radio._resolve_rx_target(None)
        self.assertEqual(rx_id, 0)
        self.assertEqual(suffix, "")
        # After focusing RX2, None routes to RX2.
        self.radio.set_focused_rx(2)
        rx_id, suffix = self.radio._resolve_rx_target(None)
        self.assertEqual(rx_id, 2)
        self.assertEqual(suffix, "_rx2")

    def test_resolve_rx_target_explicit_rx1(self) -> None:
        # Explicit RX1 (0) always returns RX1 regardless of focus.
        self.radio.set_focused_rx(2)
        rx_id, suffix = self.radio._resolve_rx_target(0)
        self.assertEqual(rx_id, 0)
        self.assertEqual(suffix, "")

    def test_resolve_rx_target_explicit_rx2(self) -> None:
        # Explicit RX2 (2) always returns RX2 regardless of focus.
        rx_id, suffix = self.radio._resolve_rx_target(2)
        self.assertEqual(rx_id, 2)
        self.assertEqual(suffix, "_rx2")

    def test_resolve_rx_target_invalid_raises(self) -> None:
        for bad in (1, 3, -1, 99):
            with self.subTest(rx_id=bad):
                with self.assertRaises(ValueError):
                    self.radio._resolve_rx_target(bad)

    # ── Phase 2 fan-out preserves the lock-step invariant ───────────

    def test_set_mode_keeps_per_rx_state_in_lockstep(self) -> None:
        """Phase 3.A invariant: ``_mode_rx2`` follows ``_mode``
        when the existing (Phase 2) fan-out setters run.  Phase
        3.B introduces target_rx semantics that let them diverge."""
        original = self.radio._mode
        # Pick a different mode that's in ALL_MODES.
        candidate = "LSB" if original != "LSB" else "USB"
        self.radio.set_mode(candidate)
        self.assertEqual(self.radio._mode, candidate)
        self.assertEqual(self.radio._mode_rx2, candidate)


if __name__ == "__main__":
    unittest.main()
