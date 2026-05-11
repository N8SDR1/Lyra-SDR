"""Sentinel test for v0.1 Phase 0 item 7 (SpectrumSourceMixin).

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §3.1.x item 7:

  > Verified: unit test in ``tests/ui/test_spectrum_source_switch.py``
  > instantiates each widget and calls
  > ``set_source(SourceID.TX_BASEBAND, lambda x: None)`` without
  > exception.

Validates that:

1. The mixin's class-level state (no ``__init__`` override) means Qt
   MI doesn't break either widget's construction -- this is the
   Round 4 Agent G probe that pinned the "plain Python mixin, not
   QObject" decision.
2. Python's C3 linearization picks the right MRO for both
   ``(SpectrumSourceMixin, _PaintedWidget)`` and
   ``(SpectrumSourceMixin, QOpenGLWidget)`` declarations.
3. ``set_source(...)`` runs without exception on a fresh widget
   instance (Phase 2's source-switch entry point works at the
   surface level even though no producer is wired yet).
4. The read accessors round-trip the values set via ``set_source``.
5. Type checking on ``source_id`` rejects bare strings (defends
   against plan-misreading).
"""
from __future__ import annotations

import sys
import unittest

from PySide6.QtWidgets import QApplication

from lyra.ui.spectrum_common import SourceID, SpectrumSourceMixin


def _ensure_qapp() -> QApplication:
    """One QApplication per test process -- Qt widgets cannot
    instantiate without one.  Cached so calls from successive
    test methods don't try to construct a second one (illegal).
    """
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


class SpectrumSourceMixinUnitTest(unittest.TestCase):
    """Mixin-level tests that don't require Qt widget construction."""

    def test_mixin_is_plain_python_not_qobject(self) -> None:
        """The R5-4 / Round 4 Agent G pin: mixin MUST NOT be a
        ``QObject`` subclass -- Qt MI with two QObject bases is
        illegal and would break both widget classes.
        """
        from PySide6.QtCore import QObject
        self.assertFalse(
            issubclass(SpectrumSourceMixin, QObject),
            "SpectrumSourceMixin must be plain Python, not QObject "
            "(see lyra/ui/spectrum_common.py module docstring)",
        )

    def test_source_id_members(self) -> None:
        """Plan §3.3 M-2 SourceID enum members -- order doesn't
        matter, the four members do.
        """
        members = {m.value for m in SourceID}
        self.assertEqual(
            members,
            {"rx1_band", "rx2_band", "tx_baseband", "ps_feedback"},
        )

    def test_set_source_type_check(self) -> None:
        """Defensive: a bare string for ``source_id`` is rejected
        with a clear TypeError, not silently accepted (which would
        break Phase 2 dispatch logic at runtime in confusing ways).
        """
        # Use the mixin directly via a thin shell -- no Qt needed
        # for this assertion.
        class _Shell(SpectrumSourceMixin):
            pass

        s = _Shell()
        with self.assertRaises(TypeError):
            s.set_source("rx1_band", None)        # type: ignore[arg-type]


class SpectrumWidgetSourceSwitchTest(unittest.TestCase):
    """Sentinel: ``SpectrumWidget`` honors the mixin contract."""

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_qapp()

    def test_instantiates_and_sets_source(self) -> None:
        """The plan's exact sentinel wording: instantiate the
        widget, call ``set_source(SourceID.TX_BASEBAND, lambda x:
        None)`` without exception.
        """
        from lyra.ui.spectrum import SpectrumWidget
        w = SpectrumWidget()
        try:
            w.set_source(SourceID.TX_BASEBAND, lambda x: None)
        finally:
            w.deleteLater()

    def test_source_roundtrip(self) -> None:
        """Read accessors return what was set."""
        from lyra.ui.spectrum import SpectrumWidget
        w = SpectrumWidget()
        try:
            dispatch_calls = []
            cb = lambda samples: dispatch_calls.append(samples)
            w.set_source(SourceID.PS_FEEDBACK, cb)
            self.assertEqual(w.active_source_id, SourceID.PS_FEEDBACK)
            self.assertIs(w.active_dispatch_fn, cb)
        finally:
            w.deleteLater()

    def test_default_source_is_rx1_band(self) -> None:
        """Fresh widget, no set_source call: lazy-init default is
        ``RX1_BAND`` with no dispatch callable wired.  Preserves
        v0.0.x parity -- the panadapter behavior on a fresh launch
        is unchanged.
        """
        from lyra.ui.spectrum import SpectrumWidget
        w = SpectrumWidget()
        try:
            self.assertEqual(w.active_source_id, SourceID.RX1_BAND)
            self.assertIsNone(w.active_dispatch_fn)
        finally:
            w.deleteLater()


class SpectrumGpuWidgetSourceSwitchTest(unittest.TestCase):
    """Sentinel: ``SpectrumGpuWidget`` honors the mixin contract.

    Skipped if QtOpenGLWidgets isn't importable on this runner --
    matches the gfx.py fallback chain so CI on headless environments
    that lack an OpenGL context doesn't fail spuriously.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_qapp()

    def _import_or_skip(self):
        try:
            from lyra.ui.spectrum_gpu import SpectrumGpuWidget
        except ImportError as e:
            self.skipTest(f"SpectrumGpuWidget unavailable: {e}")
        return SpectrumGpuWidget

    def test_instantiates_and_sets_source(self) -> None:
        SpectrumGpuWidget = self._import_or_skip()
        w = SpectrumGpuWidget()
        try:
            w.set_source(SourceID.TX_BASEBAND, lambda x: None)
        finally:
            w.deleteLater()

    def test_source_roundtrip(self) -> None:
        SpectrumGpuWidget = self._import_or_skip()
        w = SpectrumGpuWidget()
        try:
            cb = lambda samples: None
            w.set_source(SourceID.RX2_BAND, cb)
            self.assertEqual(w.active_source_id, SourceID.RX2_BAND)
            self.assertIs(w.active_dispatch_fn, cb)
        finally:
            w.deleteLater()


if __name__ == "__main__":
    unittest.main()
