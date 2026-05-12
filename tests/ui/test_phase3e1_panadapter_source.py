"""Phase 3.E.1 v0.1 -- panadapter follows focused RX.

Operator UX (2026-05-12): when the operator clicks VFO B's LED or
hits Ctrl+2, the panadapter should retune to RX2's band so they
can see the signal they're listening to.  Adds a
``panadapter_source_rx`` state on Radio that auto-tracks
``focused_rx`` by default.

Phase 3.E.2 will add a "TX override" path (panadapter stays on
TX VFO during MOX regardless of focus); for now the source
strictly follows focus.

Run from repo root::

    python -m unittest tests.ui.test_phase3e1_panadapter_source -v
"""
from __future__ import annotations

import sys
import unittest


class Phase3e1PanadapterSourceTest(unittest.TestCase):
    """``panadapter_source_rx`` state + auto-track behavior."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    # ── State surface ──────────────────────────────────────────────

    def test_default_source_is_rx1(self) -> None:
        self.assertEqual(self.radio.panadapter_source_rx, 0)

    def test_set_panadapter_source_rx_valid(self) -> None:
        self.radio.set_panadapter_source_rx(2)
        self.assertEqual(self.radio.panadapter_source_rx, 2)
        self.radio.set_panadapter_source_rx(0)
        self.assertEqual(self.radio.panadapter_source_rx, 0)

    def test_set_panadapter_source_rx_validates(self) -> None:
        for bad in (1, 3, -1, 99):
            with self.subTest(rx_id=bad):
                with self.assertRaises(ValueError):
                    self.radio.set_panadapter_source_rx(bad)

    def test_set_panadapter_source_rx_idempotent(self) -> None:
        """Setting to current value must NOT emit the signal."""
        seen: list[int] = []
        self.radio.panadapter_source_changed.connect(seen.append)
        self.radio.set_panadapter_source_rx(0)  # already 0
        self.assertEqual(seen, [])

    def test_panadapter_source_changed_emits_on_transition(self) -> None:
        seen: list[int] = []
        self.radio.panadapter_source_changed.connect(seen.append)
        self.radio.set_panadapter_source_rx(2)
        self.assertEqual(seen, [2])
        self.radio.set_panadapter_source_rx(0)
        self.assertEqual(seen, [2, 0])

    # ── Auto-track from focused_rx ─────────────────────────────────

    def test_focus_change_to_rx2_updates_panadapter_source(self) -> None:
        self.radio.set_focused_rx(2)
        self.assertEqual(self.radio.panadapter_source_rx, 2)

    def test_focus_change_back_to_rx1_restores_source(self) -> None:
        self.radio.set_focused_rx(2)
        self.radio.set_focused_rx(0)
        self.assertEqual(self.radio.panadapter_source_rx, 0)

    def test_focus_emits_both_signals(self) -> None:
        """A focus change should emit BOTH focused_rx_changed AND
        panadapter_source_changed -- panels that bind to either
        signal stay in sync."""
        focus_seen: list[int] = []
        source_seen: list[int] = []
        self.radio.focused_rx_changed.connect(focus_seen.append)
        self.radio.panadapter_source_changed.connect(source_seen.append)
        self.radio.set_focused_rx(2)
        self.assertIn(2, focus_seen)
        self.assertIn(2, source_seen)

    # ── Click-to-tune routes to source RX ──────────────────────────

    def test_click_to_tune_routes_to_source_rx2(self) -> None:
        self.radio.set_panadapter_source_rx(2)
        orig_rx1 = self.radio.freq_hz
        # Round to a value that survives Exact rounding (it should
        # round to itself).
        self.radio.set_freq_from_panadapter(7_200_000)
        self.assertEqual(self.radio.rx2_freq_hz, 7_200_000)
        self.assertEqual(self.radio.freq_hz, orig_rx1)

    def test_click_to_tune_routes_to_source_rx1(self) -> None:
        self.radio.set_panadapter_source_rx(0)
        orig_rx2 = self.radio.rx2_freq_hz
        self.radio.set_freq_from_panadapter(14_205_000)
        self.assertEqual(self.radio.freq_hz, 14_205_000)
        self.assertEqual(self.radio.rx2_freq_hz, orig_rx2)


class Phase3e1Rx2EnqueueGateRegressionTest(unittest.TestCase):
    """Regression for the 2026-05-12 SUB-off-blocks-RX2-FFT bug.

    Phase 3.D safety belt (f6470ae) gated RX2 enqueue on
    ``rx2_enabled``.  When Phase 3.E.1 added "panadapter follows
    focus", clicking RX2's LED with SUB off updated the center
    freq label but the FFT pipeline kept feeding RX1 samples
    because RX2 IQ never reached the worker queue.

    Fix: removed the enqueue gate.  The worker's audio dispatch
    (7923b94) is the real safety belt for "SUB off = no RX2
    audio"; sample queueing is independent of audio routing.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_rx2_enqueue_runs_when_sub_off(self) -> None:
        """``_stream_cb_rx2`` must enqueue RX2 samples to the worker
        regardless of SUB state, so the FFT pipeline can read them
        when ``panadapter_source_rx == 2``."""
        import numpy as np
        # Force-build a mock worker that records enqueue calls.
        calls = []

        class _MockWorker:
            def enqueue_iq_rx2(self, samples):
                calls.append(len(samples))

        self.radio._dsp_worker = _MockWorker()
        # Mimic worker-mode startup so the threading-mode gate
        # passes.
        self.radio._dsp_threading_mode_at_startup = (
            self.radio.DSP_THREADING_WORKER)
        # SUB explicitly OFF -- the bug case.
        self.radio.set_rx2_enabled(False)
        # Feed enough RX2 IQ samples to exceed the batch size and
        # trigger an enqueue (mirrors the EP6 parser dispatching
        # ``_stream_cb_rx2`` per UDP datagram).
        batch_size = int(self.radio._rx_batch_size)
        n = batch_size + 16
        samples = np.zeros(n, dtype=np.complex64)
        self.radio._stream_cb_rx2(samples, None)
        self.assertTrue(
            calls,
            "RX2 samples must be enqueued even with SUB off so the "
            "panadapter FFT can use them when source = RX2.",
        )


class Phase3e1WorkerFlushTest(unittest.TestCase):
    """Worker has a ``flush_fft_ring`` method that the source-change
    signal triggers so the next FFT frame is clean."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def test_worker_has_flush_method(self) -> None:
        from lyra.dsp.worker import DspWorker
        self.assertTrue(hasattr(DspWorker, "flush_fft_ring"))

    def test_flush_is_safe_when_ring_uninitialised(self) -> None:
        """Worker lazy-initializes the ring on first sample; flush
        before that must be a no-op (no AttributeError)."""
        from lyra.dsp.worker import DspWorker
        w = DspWorker()
        try:
            w.flush_fft_ring()  # should not raise
        finally:
            try:
                w.stop()
            except Exception:
                pass

    def test_flush_clears_ring_and_block_counter(self) -> None:
        from lyra.dsp.worker import DspWorker
        from collections import deque
        w = DspWorker()
        try:
            w._sample_ring = deque(range(10), maxlen=100)
            w._fft_block_counter = 5
            w.flush_fft_ring()
            self.assertEqual(len(w._sample_ring), 0)
            self.assertEqual(w._fft_block_counter, 0)
        finally:
            try:
                w.stop()
            except Exception:
                pass


class Phase3e1SubOffFocusFlipMirrorTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.3 (2026-05-12) -- when SUB is OFF, the
    focused RX is the only audible source, so its Vol/Mute slider
    IS the operative output control.  Flipping focus must carry
    the previously-active level forward so the operator never gets
    a surprise blast from a stale per-RX default.

    Per-RX volume + mute independence is preserved when SUB is ON.
    AF gain is NOT mirrored -- it's a pre-AGC reference and the
    Phase 3.C per-RX-AF-gain contract still holds.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_sub_off_focus_flip_rx1_to_rx2_mirrors_volume(self) -> None:
        self.radio.set_volume(0.2)  # operator trims RX1 way down
        self.radio.set_focused_rx(2)
        self.assertAlmostEqual(self.radio._volume_rx2, 0.2, places=6)

    def test_sub_off_focus_flip_rx2_to_rx1_mirrors_volume(self) -> None:
        self.radio.set_focused_rx(2)
        # Operator cranks RX2 hot while focused on it.
        self.radio.set_volume(0.9, target_rx=2)
        self.radio.set_focused_rx(0)
        self.assertAlmostEqual(self.radio._volume, 0.9, places=6)

    def test_sub_off_focus_flip_mirrors_mute(self) -> None:
        self.radio.set_muted(True)
        self.radio.set_focused_rx(2)
        self.assertTrue(self.radio._muted_rx2)
        # And unmuting on RX2 follows back to RX1.
        self.radio.set_muted(False, target_rx=2)
        self.radio.set_focused_rx(0)
        self.assertFalse(self.radio._muted)

    def test_sub_on_focus_flip_does_NOT_mirror_volume(self) -> None:
        """Per consensus plan §6.8, with SUB enabled the operator
        sees separate Vol-A / Vol-B sliders and they're independent
        by design."""
        self.radio.set_rx2_enabled(True)
        # SUB-on rising edge already mirrored RX1->RX2; now diverge.
        self.radio.set_volume(0.3, target_rx=0)
        self.radio.set_volume(0.7, target_rx=2)
        self.radio.set_focused_rx(2)
        # Both values must be preserved.
        self.assertAlmostEqual(self.radio._volume, 0.3, places=6)
        self.assertAlmostEqual(self.radio._volume_rx2, 0.7, places=6)

    def test_sub_off_focus_flip_does_NOT_mirror_af_gain(self) -> None:
        """Phase 3.C per-RX AF gain independence holds even with
        SUB off.  AF gain is pre-AGC reference, doesn't drive the
        ``surprise blast`` safety concern that volume does."""
        self.radio.set_af_gain_db(15, target_rx=0)
        self.radio.set_af_gain_db(40, target_rx=2)
        self.radio.set_focused_rx(2)
        self.assertEqual(self.radio._af_gain_db, 15)
        self.assertEqual(self.radio._af_gain_db_rx2, 40)


class Phase3e1BandRecallRoutesToFocusTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.4 (2026-05-12) -- band buttons follow
    focused VFO.  Operator UX: "if on RX2 and I click the band
    button shouldn't I be able to have that go to RX2.  Currently
    if I click band button with RX2 highlighted GREEN active the
    band changes go to RX1."

    Fix: ``recall_band`` accepts ``target_rx`` (default = focused
    RX) and dispatches freq + mode writes to the right channel.
    LNA gain stays shared (single HL2 ADC); band memory key stays
    shared (per-RX band memory is a Phase 4+ concern).
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_recall_band_default_routes_to_rx1_on_default_focus(self) -> None:
        orig_rx2 = self.radio.rx2_freq_hz
        self.radio.recall_band("40m", 7_074_000, "USB")
        self.assertEqual(self.radio.freq_hz, 7_074_000)
        self.assertEqual(self.radio.rx2_freq_hz, orig_rx2)

    def test_recall_band_routes_to_rx2_when_focused(self) -> None:
        self.radio.set_focused_rx(2)
        orig_rx1 = self.radio.freq_hz
        self.radio.recall_band("20m", 14_205_000, "USB")
        self.assertEqual(self.radio.rx2_freq_hz, 14_205_000)
        self.assertEqual(self.radio.freq_hz, orig_rx1)

    def test_recall_band_routes_mode_to_focused_rx(self) -> None:
        self.radio.set_focused_rx(2)
        orig_rx1_mode = self.radio._mode
        self.radio.recall_band("40m", 7_074_000, "LSB")
        self.assertEqual(self.radio._mode_rx2, "LSB")
        self.assertEqual(self.radio._mode, orig_rx1_mode)

    def test_recall_band_explicit_target_rx_overrides_focus(self) -> None:
        self.radio.set_focused_rx(2)
        orig_rx2 = self.radio.rx2_freq_hz
        self.radio.recall_band(
            "20m", 14_205_000, "USB", target_rx=0)
        self.assertEqual(self.radio.freq_hz, 14_205_000)
        self.assertEqual(self.radio.rx2_freq_hz, orig_rx2)

    def test_band_memory_save_after_rx2_recall_records_rx2_freq(self) -> None:
        """When the operator clicks a band button while focused on
        RX2, the post-tune band-memory save must capture RX2's
        freq+mode -- not clobber the band's memory slot with RX1's
        unrelated state."""
        self.radio.set_focused_rx(2)
        self.radio.recall_band("40m", 7_074_000, "USB")
        # Tune RX2 to a custom freq inside 40m (will auto-save? no,
        # set_rx2_freq_hz doesn't auto-save; force the save path).
        self.radio.set_rx2_freq_hz(7_200_000)
        self.radio._save_current_band_memory(target_rx=2)
        mem = self.radio._band_memory.get("40m", {})
        self.assertEqual(mem.get("freq_hz"), 7_200_000)


class Phase3e1TunePresetRoutesToFocusTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.5 (2026-05-12) -- ``Radio.tune_preset``
    is the band-panel atomic preset tune used by GEN slots, TIME
    button, TIME menu picks, and Memory recall.  Routes freq +
    mode + optional RX BW write to the focused RX (or explicit
    ``target_rx``) so every band-panel button follows VFO focus.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_tune_preset_default_routes_to_rx1_on_default_focus(self) -> None:
        orig_rx2 = self.radio.rx2_freq_hz
        self.radio.tune_preset(14_205_000, "USB")
        self.assertEqual(self.radio.freq_hz, 14_205_000)
        self.assertEqual(self.radio._mode, "USB")
        self.assertEqual(self.radio.rx2_freq_hz, orig_rx2)

    def test_tune_preset_routes_to_rx2_when_focused(self) -> None:
        self.radio.set_focused_rx(2)
        orig_rx1 = self.radio.freq_hz
        self.radio.tune_preset(7_074_000, "LSB")
        self.assertEqual(self.radio.rx2_freq_hz, 7_074_000)
        self.assertEqual(self.radio._mode_rx2, "LSB")
        self.assertEqual(self.radio.freq_hz, orig_rx1)

    def test_tune_preset_with_rx_bw_pin(self) -> None:
        self.radio.tune_preset(7_074_000, "USB", rx_bw_hz=1800)
        self.assertEqual(self.radio._rx_bw_by_mode.get("USB"), 1800)

    def test_tune_preset_explicit_target_overrides_focus(self) -> None:
        self.radio.set_focused_rx(2)
        orig_rx2 = self.radio.rx2_freq_hz
        self.radio.tune_preset(14_205_000, "USB", target_rx=0)
        self.assertEqual(self.radio.freq_hz, 14_205_000)
        self.assertEqual(self.radio.rx2_freq_hz, orig_rx2)


class Phase3e1BandPanelHighlightTracksFocusTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.6 (2026-05-12) -- BandPanel's
    band-button highlight + GEN-slot auto-save follow the focused
    VFO instead of being permanently tied to RX1.

    Three sub-behaviors:

    * Focus flip refreshes the highlighted band button to match
      the newly-focused RX's frequency.
    * GEN slots remember which RX "owns" them (set at click
      time); freq tweaks on that RX auto-save into the slot,
      tweaks on the OTHER RX do not.
    * Tuning into a structured band clears the active GEN slot.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import BandPanel
        self.radio = Radio()
        self.panel = BandPanel(self.radio)

    def _checked_band_names(self) -> list[str]:
        return [
            name for name, btn in self.panel._buttons.items()
            if btn.isChecked()
        ]

    def test_band_highlight_follows_focus_to_rx2(self) -> None:
        # RX1 tuned inside 20m default; RX2 inside 40m default.
        self.radio.set_freq_hz(14_205_000)
        self.radio.set_rx2_freq_hz(7_074_000)
        # On default focus (RX1) the 20m button should highlight.
        self.assertIn("20m", self._checked_band_names())
        self.radio.set_focused_rx(2)
        self.assertIn("40m", self._checked_band_names())
        self.assertNotIn("20m", self._checked_band_names())

    def test_band_highlight_returns_on_focus_back_to_rx1(self) -> None:
        self.radio.set_freq_hz(14_205_000)
        self.radio.set_rx2_freq_hz(7_074_000)
        self.radio.set_focused_rx(2)
        self.radio.set_focused_rx(0)
        self.assertIn("20m", self._checked_band_names())

    def test_gen_slot_owner_recorded_on_click(self) -> None:
        self.radio.set_focused_rx(2)
        # Pick the first GEN slot and point it at an out-of-band
        # freq so the band-button-takes-priority logic doesn't
        # clear ``_active_gen_rx`` immediately on tune.
        slot = next(iter(self.panel._gen_memory.keys()))
        self.panel._gen_memory[slot] = (5_500_000, "USB")
        self.panel._on_gen_clicked(slot)
        self.assertEqual(self.panel._active_gen_rx, 2)

    def test_gen_auto_save_follows_owner_rx(self) -> None:
        """Operator: focus RX2, click GEN1, then nudge RX2 freq.
        GEN1 must follow RX2.  Nudging RX1 must NOT change
        GEN1."""
        slot = next(iter(self.panel._gen_memory.keys()))
        # First, point the GEN slot's stored freq into a freq that
        # is OUTSIDE all structured bands so the auto-save path
        # isn't shadowed by band-button-takes-priority logic.  Pick
        # a quiet HF gap (say 5.5 MHz).
        self.panel._gen_memory[slot] = (5_500_000, "USB")
        self.radio.set_focused_rx(2)
        self.panel._on_gen_clicked(slot)  # tunes RX2 to 5_500_000
        # Nudge RX2 to a different out-of-band freq.
        self.radio.set_rx2_freq_hz(5_600_000)
        self.assertEqual(self.panel._gen_memory[slot][0], 5_600_000)
        # Nudge RX1 to yet another out-of-band freq -- must NOT
        # affect the slot (RX1 isn't the owner).
        self.radio.set_freq_hz(5_700_000)
        self.assertEqual(self.panel._gen_memory[slot][0], 5_600_000)

    def test_tuning_into_band_clears_active_gen(self) -> None:
        slot = next(iter(self.panel._gen_memory.keys()))
        self.panel._gen_memory[slot] = (5_500_000, "USB")
        self.panel._on_gen_clicked(slot)
        # Now tune RX1 (focused) into 20m -- band button wins.
        self.radio.set_freq_hz(14_205_000)
        self.assertIsNone(self.panel._active_gen)
        self.assertIsNone(self.panel._active_gen_rx)


class Phase3e1PanadapterMarkerClickRoutesToFocusTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.7 (2026-05-12) -- band-plan landmark
    triangles (FT8 markers, NCDXF beacons, etc.) and cluster/RBN
    spot markers must tune the panadapter-source RX, not always
    RX1.  Operator UX: "clicking say FT8 marker in panadapter
    also goes to RX1 (even if RX2 highlighted)".

    Plain panadapter click (``set_freq_from_panadapter``) was
    already fixed in Phase 3.E.1 v0.1; these are the marker-
    specific click paths that lived outside that chokepoint.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_activate_spot_near_routes_to_panadapter_source_rx2(self) -> None:
        """When RX2 owns the panadapter, clicking a cluster spot
        in that pane tunes RX2 -- not RX1."""
        self.radio._spots = {
            "K1ABC": {
                "call": "K1ABC", "mode": "USB",
                "freq_hz": 14_205_000,
            },
        }
        self.radio.set_focused_rx(2)
        orig_rx1 = self.radio.freq_hz
        ok = self.radio.activate_spot_near(14_205_100)
        self.assertTrue(ok)
        self.assertEqual(self.radio.rx2_freq_hz, 14_205_000)
        self.assertEqual(self.radio.freq_hz, orig_rx1)

    def test_activate_spot_near_default_routes_to_rx1(self) -> None:
        """When RX1 owns the panadapter (default), spots still tune
        RX1 -- legacy behavior preserved."""
        self.radio._spots = {
            "K1ABC": {
                "call": "K1ABC", "mode": "USB",
                "freq_hz": 14_205_000,
            },
        }
        orig_rx2 = self.radio.rx2_freq_hz
        ok = self.radio.activate_spot_near(14_205_100)
        self.assertTrue(ok)
        self.assertEqual(self.radio.freq_hz, 14_205_000)
        self.assertEqual(self.radio.rx2_freq_hz, orig_rx2)

    def test_activate_spot_emits_signal_regardless_of_target_rx(self) -> None:
        """``spot_activated`` must fire for TCI round-trip even
        when the tune goes to RX2."""
        self.radio._spots = {
            "K1ABC": {
                "call": "K1ABC", "mode": "USB",
                "freq_hz": 14_205_000,
            },
        }
        self.radio.set_focused_rx(2)
        seen: list[tuple] = []
        self.radio.spot_activated.connect(
            lambda call, mode, hz: seen.append((call, mode, hz)))
        self.radio.activate_spot_near(14_205_100)
        self.assertEqual(
            seen, [("K1ABC", "USB", 14_205_000)])


class Phase3e1Rx2CwPitchTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.8 (2026-05-12) -- CW pitch offset must
    apply to RX2's DDS (DDC1) the same way it does for RX1's DDC0
    under v0.0.9.8's carrier-freq VFO convention.

    Operator-reported bug (Rick 2026-05-12): "CW no pitch on RX2
    -- if I was on RX1 say DIGU and click RX2 and go to CWL or
    CWU".  Pre-fix, ``set_rx2_freq_hz`` wrote raw VFO Hz to the
    wire and the RX2 mode-change path skipped the DDS re-push,
    so the WDSP CW filter (correctly centered on ±pitch) had no
    signal in its passband.

    Pitch is a single global setting (operator-ear preference,
    not a per-receiver state).  Both RXes use the same value.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_compute_dds_for_rx2_cwu_subtracts_pitch(self) -> None:
        self.radio._mode_rx2 = "CWU"
        self.radio._rx2_freq_hz = 7_030_000
        self.radio._cw_pitch_hz = 650
        self.assertEqual(
            self.radio._compute_dds_freq_hz(target_rx=2),
            7_030_000 - 650,
        )

    def test_compute_dds_for_rx2_cwl_adds_pitch(self) -> None:
        self.radio._mode_rx2 = "CWL"
        self.radio._rx2_freq_hz = 3_530_000
        self.radio._cw_pitch_hz = 700
        self.assertEqual(
            self.radio._compute_dds_freq_hz(target_rx=2),
            3_530_000 + 700,
        )

    def test_compute_dds_for_rx2_non_cw_is_identity(self) -> None:
        self.radio._mode_rx2 = "USB"
        self.radio._rx2_freq_hz = 14_205_000
        self.assertEqual(
            self.radio._compute_dds_freq_hz(target_rx=2),
            14_205_000,
        )

    def test_compute_dds_target_rx_does_not_disturb_rx1(self) -> None:
        """Asking for RX2's DDS must not read RX1's mode/freq."""
        self.radio._mode = "USB"
        self.radio._freq_hz = 14_205_000
        self.radio._mode_rx2 = "CWU"
        self.radio._rx2_freq_hz = 7_030_000
        self.radio._cw_pitch_hz = 600
        self.assertEqual(
            self.radio._compute_dds_freq_hz(target_rx=0), 14_205_000)
        self.assertEqual(
            self.radio._compute_dds_freq_hz(target_rx=2),
            7_030_000 - 600)

    def test_set_rx2_freq_hz_applies_pitch_offset_via_stream(self) -> None:
        """``set_rx2_freq_hz`` writes the DDS-offset value to the
        protocol layer, not the raw VFO Hz."""
        seen: list[int] = []

        class _MockStream:
            def _set_rx2_freq(self, hz):
                seen.append(int(hz))

            def _set_rx1_freq(self, hz):
                pass

        self.radio._stream = _MockStream()
        self.radio._mode_rx2 = "CWU"
        self.radio._cw_pitch_hz = 650
        self.radio.set_rx2_freq_hz(7_030_000)
        self.assertEqual(seen, [7_030_000 - 650])

    def test_rx2_mode_change_to_cw_repushes_dds_with_pitch(self) -> None:
        """Changing RX2 from DIGU to CWU re-pushes DDC1 freq with
        the pitch offset.  Pre-fix this step was missing and the
        operator heard silence."""
        seen: list[int] = []

        class _MockStream:
            def _set_rx2_freq(self, hz):
                seen.append(int(hz))

            def _set_rx1_freq(self, hz):
                pass

        self.radio._stream = _MockStream()
        self.radio._cw_pitch_hz = 600
        self.radio._mode_rx2 = "DIGU"
        self.radio._rx2_freq_hz = 7_074_000
        # Now flip to CWU.
        self.radio.set_mode("CWU", target_rx=2)
        self.assertIn(7_074_000 - 600, seen)

    def test_cw_pitch_change_repushes_rx2_dds_when_on_cw(self) -> None:
        """Dialing the pitch dial mid-session must update RX2's
        DDS when RX2 is on CW (mirror of the existing RX1
        behavior at line ~9733)."""
        rx2_writes: list[int] = []

        class _MockStream:
            def _set_rx2_freq(self, hz):
                rx2_writes.append(int(hz))

            def _set_rx1_freq(self, hz):
                pass

        self.radio._stream = _MockStream()
        self.radio._mode_rx2 = "CWU"
        self.radio._rx2_freq_hz = 7_030_000
        self.radio._cw_pitch_hz = 600
        self.radio.set_cw_pitch_hz(800)
        self.assertIn(7_030_000 - 800, rx2_writes)


class Phase3e1CwPitchOnTuningPanelTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.9 (2026-05-12) -- CW Pitch control was
    moved from ModeFilterPanel to TuningPanel per operator UX call.
    Pitch is tuning-adjacent (you zero-beat against it) and shared
    across both VFOs.  Row hides when neither RX is on CW.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import TuningPanel, ModeFilterPanel
        self.radio = Radio()
        self.tuning = TuningPanel(self.radio)
        self.mode_filter = ModeFilterPanel(self.radio)

    def test_tuning_panel_has_cw_pitch_widget(self) -> None:
        self.assertTrue(hasattr(self.tuning, "cw_pitch_spin"))
        self.assertTrue(hasattr(self.tuning, "cw_pitch_row"))

    def test_mode_filter_panel_no_longer_has_cw_pitch_widget(self) -> None:
        """Regression marker: the widget should NOT exist on
        ModeFilterPanel after the move.  If a future refactor
        adds it back, this test catches the dup."""
        self.assertFalse(hasattr(self.mode_filter, "cw_pitch_spin"))

    def test_cw_pitch_always_visible_regardless_of_mode(self) -> None:
        """Phase 3.E.1 hotfix v0.11 (2026-05-12): operator UX call --
        leave CW Pitch visible in all modes, not hidden outside CW.
        Pitch is an operator-tuned parameter that operators may
        want to set/preview while ON other modes too."""
        # Both VFOs non-CW: still visible.
        self.radio.set_mode("USB", target_rx=0)
        self.radio.set_mode("USB", target_rx=2)
        self.tuning._update_cw_pitch_visibility()
        self.assertFalse(self.tuning.cw_pitch_label.isHidden())
        self.assertFalse(self.tuning.cw_pitch_spin.isHidden())
        # RX1 on CW: still visible.
        self.radio.set_mode("CWU", target_rx=0)
        self.tuning._update_cw_pitch_visibility()
        self.assertFalse(self.tuning.cw_pitch_label.isHidden())
        # Flip back: still visible.
        self.radio.set_mode("USB", target_rx=0)
        self.tuning._update_cw_pitch_visibility()
        self.assertFalse(self.tuning.cw_pitch_label.isHidden())

    def test_spin_change_writes_radio_cw_pitch(self) -> None:
        self.tuning.cw_pitch_spin.setValue(720)
        self.assertEqual(self.radio.cw_pitch_hz, 720)

    def test_radio_pitch_change_updates_spin(self) -> None:
        self.radio.set_cw_pitch_hz(880)
        self.assertEqual(self.tuning.cw_pitch_spin.value(), 880)


class Phase3e1Rx2PanadapterCwCenterTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.12 (2026-05-12) -- RX2 click-to-tune
    on CW panadapter peaks must land on the carrier (not off by
    one pitch).  Operator report 2026-05-12: "RX2 clicking on CW
    in panadapter doesn't tune the same way RX1 does, RX2 seems
    off (almost like not accounting for the CW pitch perhaps?)".

    Root cause: the spectrum_ready emit was using
    ``self._rx2_freq_hz`` as center_hz for RX2 panes, but since
    hotfix v0.8 RX2's DDS is offset by ±pitch in CW.  The bins
    were centered on DDS_RX2 = VFO_RX2 ∓ pitch, but the widget
    thought they were centered on VFO_RX2 -- click-to-tune
    produced a freq off by exactly one pitch.

    Fix: use ``_compute_dds_freq_hz(target_rx=2)`` for the spec
    center, plus make ``marker_offset_hz`` source-RX-aware so
    the visible marker still draws at the operator's tuned
    carrier (same trick RX1 uses).
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_marker_offset_for_rx2_cwu(self) -> None:
        """Panadapter source = RX2, RX2 on CWU -> marker_offset
        should be +pitch (marker drawn to the right of DDS-centered
        bins, at the operator's tuned carrier)."""
        self.radio._mode_rx2 = "CWU"
        self.radio._rx2_freq_hz = 7_030_000
        self.radio._cw_pitch_hz = 650
        self.radio._panadapter_source_rx = 2
        self.assertEqual(self.radio.marker_offset_hz, 650)

    def test_marker_offset_for_rx2_cwl(self) -> None:
        self.radio._mode_rx2 = "CWL"
        self.radio._rx2_freq_hz = 3_530_000
        self.radio._cw_pitch_hz = 700
        self.radio._panadapter_source_rx = 2
        self.assertEqual(self.radio.marker_offset_hz, -700)

    def test_marker_offset_for_rx2_non_cw_is_zero(self) -> None:
        self.radio._mode_rx2 = "USB"
        self.radio._rx2_freq_hz = 14_205_000
        self.radio._panadapter_source_rx = 2
        self.assertEqual(self.radio.marker_offset_hz, 0)

    def test_marker_offset_switches_on_panadapter_source_flip(
        self,
    ) -> None:
        """RX1 on DIGU (offset=0), RX2 on CWU (offset=+pitch).
        Flipping panadapter source should flip the offset."""
        self.radio._mode = "DIGU"
        self.radio._freq_hz = 14_074_000
        self.radio._mode_rx2 = "CWU"
        self.radio._rx2_freq_hz = 7_030_000
        self.radio._cw_pitch_hz = 600
        # Source=RX1: marker is at the carrier (offset 0 for DIGU).
        self.radio._panadapter_source_rx = 0
        self.assertEqual(self.radio.marker_offset_hz, 0)
        # Source=RX2: marker shifts to +pitch.
        self.radio._panadapter_source_rx = 2
        self.assertEqual(self.radio.marker_offset_hz, 600)

    def test_marker_offset_re_emits_on_panadapter_source_change(
        self,
    ) -> None:
        seen: list[int] = []
        self.radio.marker_offset_changed.connect(seen.append)
        self.radio._mode_rx2 = "CWU"
        self.radio._rx2_freq_hz = 7_030_000
        self.radio._cw_pitch_hz = 650
        self.radio.set_panadapter_source_rx(2)
        # Should have fired the marker_offset_changed signal at
        # least once during the source switch.
        self.assertIn(650, seen)


class Phase3e1PassbandFollowsPanadapterSourceTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.14 (2026-05-12) -- the passband
    overlay (cyan rectangle on the spectrum) must follow the
    panadapter-source RX's mode + BW, not RX1's hard-coded.

    Operator report 2026-05-12: "look at CWL on the RX2 -- we
    dropped that extra pitch line for both Lower and Upper CW.
    RX1 is correct.  RX2 CWU (upper CW) is correct.  RX2 CWL has
    that line."

    Root cause: ``_compute_passband`` read ``self._mode`` only,
    so on an RX2 panadapter the passband rectangle was drawn for
    RX1's mode (e.g. RX1=CWU at +pitch overlay) even when RX2
    was on CWL (rectangle should be at -pitch).  Visually
    appeared as an extra cyan rectangle on the wrong side of
    the marker.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_passband_for_rx2_cwu_when_source_is_rx2(self) -> None:
        self.radio._mode_rx2 = "CWU"
        self.radio._rx_bw_by_mode_rx2["CWU"] = 250
        self.radio._cw_pitch_hz = 650
        self.radio._panadapter_source_rx = 2
        lo, hi = self.radio._compute_passband()
        # CWU @ pitch=650, bw=250 -> half=125 -> (525, 775).
        self.assertEqual((lo, hi), (525, 775))

    def test_passband_for_rx2_cwl_when_source_is_rx2(self) -> None:
        self.radio._mode_rx2 = "CWL"
        self.radio._rx_bw_by_mode_rx2["CWL"] = 250
        self.radio._cw_pitch_hz = 700
        self.radio._panadapter_source_rx = 2
        lo, hi = self.radio._compute_passband()
        # CWL @ pitch=700, bw=250 -> half=125 -> (-825, -575).
        self.assertEqual((lo, hi), (-825, -575))

    def test_passband_uses_rx1_mode_when_source_is_rx1(self) -> None:
        """Default (source = RX1) behavior preserved -- the
        passband reads RX1's mode + BW dict."""
        self.radio._mode = "USB"
        self.radio._rx_bw_by_mode["USB"] = 2400
        self.radio._mode_rx2 = "CWL"  # different from RX1
        self.radio._panadapter_source_rx = 0
        lo, hi = self.radio._compute_passband()
        self.assertEqual((lo, hi), (0, 2400))

    def test_passband_switches_with_panadapter_source(self) -> None:
        self.radio._mode = "USB"
        self.radio._rx_bw_by_mode["USB"] = 2400
        self.radio._mode_rx2 = "CWL"
        self.radio._rx_bw_by_mode_rx2["CWL"] = 500
        self.radio._cw_pitch_hz = 600
        # Source = RX1 -> USB passband.
        self.radio._panadapter_source_rx = 0
        self.assertEqual(self.radio._compute_passband(), (0, 2400))
        # Source = RX2 -> CWL passband centered on -pitch.
        self.radio._panadapter_source_rx = 2
        self.assertEqual(self.radio._compute_passband(), (-850, -350))

    def test_passband_re_emits_on_source_switch(self) -> None:
        seen: list[tuple[int, int]] = []
        self.radio.passband_changed.connect(
            lambda lo, hi: seen.append((lo, hi)))
        self.radio._mode = "USB"
        self.radio._rx_bw_by_mode["USB"] = 2400
        self.radio._mode_rx2 = "CWU"
        self.radio._rx_bw_by_mode_rx2["CWU"] = 250
        self.radio._cw_pitch_hz = 650
        self.radio.set_panadapter_source_rx(2)
        # CWU @ pitch=650 bw=250 -> (525, 775).
        self.assertIn((525, 775), seen)


class Phase3e1DualVolSlidersAlwaysVisibleTest(unittest.TestCase):
    """Phase 3.E.1 hotfix v0.16 (2026-05-12) -- always-visible
    Vol-A + Vol-B with direct addressing.

    Operator UX call 2026-05-12: "do you think it would be better
    just have two volume sliders and mutes always visible?" ->
    "A it is".  Both volume sliders and both mute buttons are
    now visible regardless of SUB state.  Vol-A is hard-wired to
    RX1, Vol-B to RX2 in both modes.

    The v0.3 mirror in ``Radio.set_focused_rx`` still keeps
    levels in sync across focus flips when SUB is off so the
    audible level doesn't jump.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import DspPanel
        self.radio = Radio()
        self.panel = DspPanel(self.radio)

    def test_vol_b_widgets_visible_with_sub_off(self) -> None:
        """Both Vol-A/Vol-B + Mute-A/Mute-B widgets always exist
        and aren't hidden when SUB is off."""
        self.assertFalse(self.panel.vol_b_slider.isHidden())
        self.assertFalse(self.panel.vol_b_label.isHidden())
        self.assertFalse(self.panel.vol_b_label_caption.isHidden())
        self.assertFalse(self.panel.mute_b_btn.isHidden())

    def test_vol_b_widgets_visible_with_sub_on(self) -> None:
        self.radio.set_rx2_enabled(True)
        self.assertFalse(self.panel.vol_b_slider.isHidden())
        self.assertFalse(self.panel.mute_b_btn.isHidden())

    def test_vol_a_label_says_vol_rx1(self) -> None:
        """Phase 3.E.1 hotfix v0.17 (2026-05-12): caption uses
        the canonical "RX1 / RX2" naming (matches VFO LED
        captions, dispatch state, focused_rx convention).  No
        relabel based on SUB state."""
        self.assertEqual(self.panel.vol_label_caption.text(), "Vol RX1")
        self.radio.set_rx2_enabled(True)
        self.assertEqual(self.panel.vol_label_caption.text(), "Vol RX1")
        self.assertEqual(
            self.panel.vol_b_label_caption.text(), "Vol RX2")

    def test_vol_a_slider_writes_rx1_always(self) -> None:
        """Vol-A always targets RX1, even when SUB is off and
        focus is RX2.  The slider is just the RX1 control."""
        # SUB off, focus RX2 (the v0.15 case that needed focus
        # routing -- no longer needed with direct addressing).
        self.radio.set_focused_rx(2)
        orig_rx2 = self.radio.volume_for_rx(2)
        self.panel.vol_slider.setValue(40)
        self.assertAlmostEqual(
            self.radio.volume_for_rx(0),
            self.panel._slider_to_volume(40), places=6)
        # RX2 untouched.
        self.assertAlmostEqual(
            self.radio.volume_for_rx(2), orig_rx2, places=6)

    def test_vol_b_slider_writes_rx2_always(self) -> None:
        orig_rx1 = self.radio.volume_for_rx(0)
        self.panel.vol_b_slider.setValue(75)
        self.assertAlmostEqual(
            self.radio.volume_for_rx(2),
            self.panel._slider_to_volume(75), places=6)
        # RX1 untouched.
        self.assertAlmostEqual(self.radio.volume_for_rx(0),
                                orig_rx1, places=6)

    def test_mute_a_writes_rx1_mute_b_writes_rx2(self) -> None:
        self.panel.mute_btn.setChecked(True)
        self.assertTrue(self.radio.muted_for_rx(0))
        self.assertFalse(self.radio.muted_for_rx(2))
        self.panel.mute_b_btn.setChecked(True)
        self.assertTrue(self.radio.muted_for_rx(2))

    def test_focus_flip_with_sub_off_still_mirrors_levels(self) -> None:
        """The v0.3 mirror in ``Radio.set_focused_rx`` still
        copies the previously-focused RX's vol -> the newly-
        focused RX when SUB is off.  This avoids an audible
        level jump when the operator flips focus."""
        self.radio.set_volume(0.2, target_rx=0)
        # Diverge RX2 to ensure mirror does something visible.
        self.radio.set_volume(0.8, target_rx=2)
        self.radio.set_focused_rx(2)
        # RX2's vol mirrored from RX1's 0.2.
        self.assertAlmostEqual(self.radio.volume_for_rx(2), 0.2,
                                places=6)
        # Vol-B slider updated via volume_changed_rx2 signal.
        sval = self.panel._volume_to_slider(0.2)
        self.assertEqual(self.panel.vol_b_slider.value(), sval)


if __name__ == "__main__":
    unittest.main()
