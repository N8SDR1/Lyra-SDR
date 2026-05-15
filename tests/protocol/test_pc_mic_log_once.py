"""Verify _wire_mic_source log-once latch (v0.2 Phase 2 commit 7.2).

Operator-bench-confirmed 2026-05-15 that an invalid PC mic device
(stale QSettings index) caused [Radio] PC mic start failed: ...
to spam the console once per Radio.start() call -- 11 prints across
11 stop/restart cycles.  Commit 7.2 latches the log to fire ONCE
per session per device config so the operator sees a clear toast
without status-bar churn.

Critical correctness gate: the latch does NOT silently fall back
to hl2_jack on failure.  Standard HL2 operators (no AK4951 codec)
have no mic on the radio at all; falling back would route to a
path that physically can't carry voice.  The operator's hardware-
source choice always wins.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock


class _StubMicSource:
    """Stand-in for SoundDeviceMicSource that raises on start()."""

    def __init__(self, raise_on_start: bool = True):
        self.raise_on_start = raise_on_start
        self.is_running: bool = False
        self.start_calls: int = 0
        self.stop_calls: int = 0

    def start(self, consumer):
        self.start_calls += 1
        if self.raise_on_start:
            raise RuntimeError(
                "Error opening InputStream: Invalid device [PaErrorCode -9996]"
            )
        self.is_running = True

    def stop(self):
        self.stop_calls += 1
        self.is_running = False


class _StubStream:
    """Stand-in for HL2Stream."""

    def __init__(self):
        self.consumer = "untouched"

    def register_mic_consumer(self, callback):
        self.consumer = callback


class _StubTxDspWorker:
    """Stand-in for TxDspWorker (only submit is called)."""

    def submit(self, samples):
        pass


class PcMicLogOnceTest(unittest.TestCase):
    """Direct unit tests on Radio._wire_mic_source."""

    def setUp(self):
        from lyra.radio import Radio
        self.r = Radio()
        # Force pc_soundcard path without going through QSettings
        self.r._mic_source = "pc_soundcard"  # noqa: SLF001
        self.r._stream = _StubStream()  # noqa: SLF001
        self.r._tx_dsp_worker = _StubTxDspWorker()  # noqa: SLF001
        self.r._pc_mic_source = _StubMicSource(  # noqa: SLF001
            raise_on_start=True,
        )
        # Spy on status_message
        self.status_msgs: list[tuple[str, int]] = []
        self.r.status_message.connect(
            lambda msg, t: self.status_msgs.append((msg, t))
        )

    def test_first_failure_emits_status_and_prints(self):
        """First _wire_mic_source with bad device → 1 status_message
        + sets the log-once latch."""
        self.assertFalse(self.r._pc_mic_failure_logged)  # noqa: SLF001
        self.r._wire_mic_source()  # noqa: SLF001
        self.assertEqual(len(self.status_msgs), 1)
        self.assertIn("PC mic unavailable", self.status_msgs[0][0])
        self.assertIn(
            "Settings -> Audio -> Mic input", self.status_msgs[0][0],
        )
        self.assertTrue(self.r._pc_mic_failure_logged)  # noqa: SLF001

    def test_subsequent_failures_silent(self):
        """Once latched, repeated failures do NOT emit status_message."""
        # First call latches
        self.r._wire_mic_source()  # noqa: SLF001
        self.assertEqual(len(self.status_msgs), 1)
        # 5 more attempts -- every Radio.start() until session ends
        for _ in range(5):
            self.r._wire_mic_source()  # noqa: SLF001
        # Still only 1 status message emitted
        self.assertEqual(len(self.status_msgs), 1)
        # And the source's start() was indeed called 6 times total
        # (proving we still retry silently for hot-plug recovery)
        self.assertEqual(
            self.r._pc_mic_source.start_calls, 6,  # noqa: SLF001
        )

    def test_successful_start_resets_latch(self):
        """If start() ever succeeds, the latch resets so a FUTURE
        failure gets a fresh toast (operator unplugs the mic mid-
        session)."""
        # First: failure latches
        self.r._wire_mic_source()  # noqa: SLF001
        self.assertTrue(self.r._pc_mic_failure_logged)  # noqa: SLF001
        # Now flip the stub to succeed (e.g., operator plugged in
        # the headset).  Reset is_running flag so _wire_mic_source
        # re-enters the start() branch.
        self.r._pc_mic_source.raise_on_start = False  # noqa: SLF001
        self.r._pc_mic_source.is_running = False  # noqa: SLF001
        self.r._wire_mic_source()  # noqa: SLF001
        # Latch cleared
        self.assertFalse(self.r._pc_mic_failure_logged)  # noqa: SLF001
        # Now break it again -- toast should fire again
        self.r._pc_mic_source.raise_on_start = True  # noqa: SLF001
        self.r._pc_mic_source.is_running = False  # noqa: SLF001
        self.r._wire_mic_source()  # noqa: SLF001
        self.assertEqual(len(self.status_msgs), 2)  # original + new

    def test_set_pc_mic_device_resets_latch(self):
        """set_pc_mic_device clears the latch so the operator's new
        choice gets fresh log treatment if it also fails."""
        # Latch first
        self.r._wire_mic_source()  # noqa: SLF001
        self.assertTrue(self.r._pc_mic_failure_logged)  # noqa: SLF001
        # Operator picks a new device via Settings UI
        self.r.set_pc_mic_device(7)
        # Latch reset (independent of whether the new device works)
        self.assertFalse(self.r._pc_mic_failure_logged)  # noqa: SLF001

    def test_set_pc_mic_channel_resets_latch(self):
        """set_pc_mic_channel clears the latch (same rationale)."""
        self.r._wire_mic_source()  # noqa: SLF001
        self.assertTrue(self.r._pc_mic_failure_logged)  # noqa: SLF001
        # Note: set_pc_mic_channel needs default _pc_mic_channel
        # to be different from the new value or it early-returns.
        # Default is "L"; pick "R".
        self.r.set_pc_mic_channel("R")
        self.assertFalse(self.r._pc_mic_failure_logged)  # noqa: SLF001

    def test_hl2_jack_source_never_touches_pc_mic(self):
        """When mic_source is hl2_jack, _wire_mic_source registers
        the HL2 consumer and does NOT touch _pc_mic_source.  This
        is the path standard-HL2 operators must NEVER end up on
        accidentally, but here we're testing the HL2+ default."""
        self.r._mic_source = "hl2_jack"  # noqa: SLF001
        self.r._wire_mic_source()  # noqa: SLF001
        # PC mic source untouched
        self.assertEqual(
            self.r._pc_mic_source.start_calls, 0,  # noqa: SLF001
        )
        # And HL2 consumer was registered (compare via ==, not is --
        # bound method objects are fresh on every attribute access).
        self.assertEqual(
            self.r._stream.consumer, self.r._on_hl2_mic,  # noqa: SLF001
        )
        # Latch unaffected
        self.assertFalse(self.r._pc_mic_failure_logged)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
