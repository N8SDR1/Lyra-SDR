"""RX2 Bench Test dialog — Phase 1 v0.1 verification surface.

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §4.4 step 2:

  > **DDC1 -> host channel 2 dispatch.**  Tune VFO B to a different
  > known carrier (e.g., WWV at 15.000 MHz).  Verify channel 2's
  > input stream shows that carrier at the expected baseband
  > offset.  **Pass:** different carrier from #1, at the right
  > frequency, in the right channel.

Phase 1 hasn't built the full RX2 UI yet (focus model + dual VFO
LEDs land in Phase 3).  This dialog is the minimum operator-facing
surface to:

* **Tune VFO B (DDC1)** to an arbitrary freq.
* **Verify dispatch** -- live counters show DDC1 samples are
  arriving at Radio's stub consumer (proves the protocol-level
  fan-out from §4.2 is wired correctly).
* **Verify carrier** -- live FFT of recent DDC1 IQ shows where the
  carrier lands at baseband; for WWV the operator can see the
  ~5 kHz BPSK + the on-the-second tick spectrum, and the peak
  freq should match (WWV_carrier - RX2_NCO_carrier).

When Phase 3 lands (dual VFO LEDs, focus model, A↔B/Swap), this
dialog can stay as a diagnostic tool or be retired -- by then the
operator has live verification through the main UI.

How to use
==========

1. Click ▶ Start on the main toolbar (the dialog refuses to do
   anything if the stream isn't running -- DDC1 samples only flow
   while EP6 is streaming).
2. Open Help -> RX2 Bench Test.
3. Pick a WWV preset button (5 / 10 / 15 / 20 MHz) or type a freq.
4. Watch the live readouts.  Expected:
   * Datagrams/sec ≈ wire IQ rate / 38 (e.g. ~5053 at 192 kHz nddc=4)
   * Samples/sec ≈ wire IQ rate (e.g. 192,000 at 192 kHz)
   * FFT peak Hz: should fall within ±(wire IQ rate / 2); a WWV
     carrier tuned exactly to one of the standard freqs (e.g., RX2
     NCO = 15,000,000 Hz with WWV 15) should show a peak near 0
     Hz baseband.  Offsetting RX2 NCO by 1 kHz from WWV's carrier
     should show the peak at ±1 kHz baseband.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QWidget,
)


# WWV broadcasts continuous time + spectral markers on these freqs
# (5 / 10 / 15 / 20 MHz are AM carriers; 25 MHz seasonal).  Used for
# the §4.4 step 2 reference signal because they're available
# 24/7/365 in most of CONUS and the carriers are extremely stable
# (NIST atomic clock + cesium reference) -- any baseband drift
# observed during this test is on Lyra's side, not WWV's.
_WWV_PRESETS_HZ = (5_000_000, 10_000_000, 15_000_000, 20_000_000)


class Rx2BenchTestDialog(QDialog):
    """Phase 1 RX2 bench-test dialog.  Non-modal: operator can leave
    it open while flipping back to the main window to tune RX1 etc.

    Usage:
        Rx2BenchTestDialog(radio, parent=window).show()
    """

    REFRESH_MS = 500   # readout refresh cadence
    FFT_SIZE = 8192    # ~23 Hz bin resolution at 192 kHz

    def __init__(self, radio, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.radio = radio
        self.setWindowTitle("RX2 Bench Test (Phase 1 v0.1)")
        self.setMinimumSize(560, 360)

        self._last_samples_total: int = 0
        self._last_datagrams_total: int = 0
        self._last_refresh_ms: int = 0

        self._build_ui()

        # Refuse gracefully if stream isn't running -- DDC1 samples
        # only flow while EP6 is up.
        streaming = bool(getattr(radio, "is_streaming", False))
        if not streaming:
            self.note.setText(
                "Stream is not running.  Click ▶ Start on the toolbar "
                "first, then this dialog will show live RX2 dispatch "
                "counters and the FFT readout."
            )

        # Flip the bench-active gate on Radio so it begins filling
        # the IQ ring buffer (skipped by default to save RX-loop
        # CPU at the 5053 dgrams/sec nddc=4 cadence).
        try:
            radio._rx2_bench_active = True  # noqa: SLF001
        except Exception:
            pass

        # Wire freq-change signal from Radio so external setters (e.g.
        # future Phase 3 UI) keep this dialog's readout in sync.
        try:
            radio.rx2_freq_changed.connect(self._on_rx2_freq_changed)
        except Exception:
            # Older Radio without the signal -- safe to ignore for
            # the dialog's purposes (operator can still type freqs in).
            pass

        # Refresh timer fires regardless of stream state so the
        # operator can see "0 datagrams" while the stream is stopped
        # and the values come alive when they hit Start.
        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    # ── UI ────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        header = QLabel(
            "<b>Phase 1 RX2 dispatch verification</b><br>"
            "Per consensus plan §4.4 step 2: tune VFO B to a known "
            "carrier (e.g., WWV at 15.000 MHz) and verify DDC1 "
            "samples arrive at the RX2 host channel with the carrier "
            "at the expected baseband offset.<br>"
            "<i>Phase 1 has no RX2 audio yet -- audio routing lands "
            "in Phase 2.</i>"
        )
        header.setWordWrap(True)
        root.addWidget(header)

        # ── Freq tune row ────────────────────────────────────────────
        tune_row = QHBoxLayout()
        tune_row.addWidget(QLabel("VFO B freq (Hz):"))
        self.freq_edit = QLineEdit()
        self.freq_edit.setPlaceholderText("e.g. 15000000")
        try:
            self.freq_edit.setText(str(int(self.radio.rx2_freq_hz)))
        except Exception:
            self.freq_edit.setText("15000000")
        self.freq_edit.returnPressed.connect(self._on_tune)
        tune_row.addWidget(self.freq_edit, 1)
        tune_btn = QPushButton("Tune RX2")
        tune_btn.clicked.connect(self._on_tune)
        tune_row.addWidget(tune_btn)
        root.addLayout(tune_row)

        # ── WWV preset row ──────────────────────────────────────────
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("WWV presets:"))
        for hz in _WWV_PRESETS_HZ:
            mhz = hz // 1_000_000
            btn = QPushButton(f"{mhz} MHz")
            btn.setToolTip(
                f"Tune VFO B to {hz:,} Hz (WWV {mhz} MHz carrier)"
            )
            btn.clicked.connect(
                lambda _checked=False, h=hz: self._tune_to(h)
            )
            preset_row.addWidget(btn)
        preset_row.addStretch(1)
        root.addLayout(preset_row)

        # ── Live readouts ───────────────────────────────────────────
        mono = QFont("Consolas")
        if not mono.exactMatch():
            mono = QFont("Courier New")
        mono.setStyleHint(QFont.Monospace)

        self.freq_readout = QLabel("Current RX2 NCO: —")
        self.freq_readout.setFont(mono)
        root.addWidget(self.freq_readout)

        self.counters_readout = QLabel(
            "Datagrams: —   Samples: —   ΔRate: —"
        )
        self.counters_readout.setFont(mono)
        root.addWidget(self.counters_readout)

        self.fft_readout = QLabel("FFT peak: —")
        self.fft_readout.setFont(mono)
        root.addWidget(self.fft_readout)

        # Operator-readable explanation of how to interpret the FFT.
        explain = QLabel(
            "<i>FFT peak should fall near 0 Hz when the RX2 NCO "
            "freq exactly matches the carrier; offsetting NCO by N "
            "Hz moves the peak to ±N Hz baseband.  WWV's 5 kHz "
            "audio modulation will also show as side energy.</i>"
        )
        explain.setWordWrap(True)
        root.addWidget(explain)

        self.note = QLabel("")
        self.note.setStyleSheet("color: #b46618;")
        self.note.setWordWrap(True)
        root.addWidget(self.note)

        # ── Close ───────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── Tune actions ──────────────────────────────────────────────────
    def _on_tune(self) -> None:
        text = self.freq_edit.text().strip()
        # Accept raw Hz, with optional thousands separators (e.g.
        # "15,000,000" or "15.000.000") and Euro/US decimal forms
        # of MHz (e.g. "15.0").  Mirrors the LED freq parser's
        # tolerant style without pulling in its full machinery.
        cleaned = text.replace(",", "").replace(" ", "")
        try:
            if "." in cleaned and cleaned.count(".") == 1:
                # Single dot -- treat as MHz.
                hz = int(round(float(cleaned) * 1_000_000))
            else:
                hz = int(cleaned)
        except Exception:
            self.note.setText(
                f"Couldn't parse '{text}' as a frequency.  "
                f"Try 15000000 or 15.000 (= 15.000 MHz)."
            )
            return
        self.note.setText("")
        self._tune_to(hz)

    def _tune_to(self, hz: int) -> None:
        try:
            self.radio.set_rx2_freq_hz(int(hz))
        except Exception as e:
            self.note.setText(f"set_rx2_freq_hz failed: {e}")
            return
        self.freq_edit.setText(str(int(hz)))

    def _on_rx2_freq_changed(self, hz: int) -> None:
        """External freq change (e.g. Phase 3 UI later) reflects
        into this dialog's edit field for consistency."""
        # Avoid clobbering the operator's in-progress edit.
        if not self.freq_edit.hasFocus():
            self.freq_edit.setText(str(int(hz)))

    # ── Periodic readout refresh ──────────────────────────────────────
    def _refresh(self) -> None:
        try:
            diag = self.radio.read_rx2_diagnostics()
        except Exception as e:
            self.fft_readout.setText(f"Diagnostics read failed: {e}")
            return

        # Freq
        self.freq_readout.setText(
            f"Current RX2 NCO: {diag['current_freq_hz']:>12,} Hz   "
            f"(IQ wire rate: {diag['iq_rate_hz']:>7,} Hz)"
        )

        # Counter delta
        dt_ms = max(1, self.REFRESH_MS)
        dt_s = dt_ms / 1000.0
        d_samples = diag["samples_total"] - self._last_samples_total
        d_datagrams = (
            diag["datagrams_total"] - self._last_datagrams_total
        )
        rate_sps = int(d_samples / dt_s) if dt_s > 0 else 0
        rate_dps = int(d_datagrams / dt_s) if dt_s > 0 else 0
        self._last_samples_total = diag["samples_total"]
        self._last_datagrams_total = diag["datagrams_total"]

        self.counters_readout.setText(
            f"Datagrams: {diag['datagrams_total']:>9,}   "
            f"Samples: {diag['samples_total']:>11,}   "
            f"ΔRate: {rate_sps:>6,} sps  ({rate_dps:,} dgrams/s)"
        )

        # FFT peak
        try:
            iq = self.radio.read_rx2_iq_snapshot()
        except Exception as e:
            self.fft_readout.setText(f"IQ read failed: {e}")
            return
        if iq.shape[0] < 64:
            self.fft_readout.setText(
                "FFT peak: — (waiting for RX2 samples)"
            )
            return
        peak_hz, peak_db = self._compute_fft_peak(iq, diag["iq_rate_hz"])
        # Anti-flap: only update if the peak shifted by >5 Hz to keep
        # the readout legible during a stable carrier.
        self.fft_readout.setText(
            f"FFT peak: {peak_hz:>+10,.1f} Hz baseband   "
            f"({peak_db:>6.1f} dBFS)"
        )

    # ── FFT helper ────────────────────────────────────────────────────
    def _compute_fft_peak(
        self, iq: np.ndarray, rate_hz: int,
    ) -> tuple[float, float]:
        """Find the dominant frequency-bin in the recent DDC1 IQ.

        Returns ``(peak_hz_baseband, peak_db_fs)``.  Baseband freq
        is signed: positive = above NCO, negative = below.

        Uses a Hann-windowed FFT of the most-recent FFT_SIZE samples
        to suppress the DC bin's spectral leakage (HL2's DC offset
        is real and would otherwise dominate the peak picker).  The
        DC bin itself (±2 bins for safety) is masked from the
        peak-search range so it never wins -- we want the carrier
        peak, not the DC peak.
        """
        n = self.FFT_SIZE
        if iq.shape[0] < n:
            # Pad with zeros for the FFT (rare; only at very start).
            buf = np.zeros(n, dtype=np.complex64)
            buf[: iq.shape[0]] = iq
        else:
            buf = iq[-n:]

        win = np.hanning(n).astype(np.float32)
        windowed = buf * win
        spectrum = np.fft.fftshift(np.fft.fft(windowed))
        mag = np.abs(spectrum)
        mag_db = 20.0 * np.log10(np.maximum(mag, 1e-12)) - 20.0 * np.log10(n / 2.0)

        # Mask out ±2 bins around DC (the centre of the shifted FFT).
        center = n // 2
        mag_search = mag.copy()
        mag_search[center - 2: center + 3] = 0.0
        peak_idx = int(np.argmax(mag_search))

        # fftshift maps bin 0 -> -rate/2, bin n-1 -> +rate/2 - bin
        # Convert peak_idx to signed freq.
        bin_hz = rate_hz / n
        peak_hz = (peak_idx - center) * bin_hz
        peak_db = float(mag_db[peak_idx])
        return float(peak_hz), peak_db

    # ── Cleanup ───────────────────────────────────────────────────────
    def closeEvent(self, event):  # noqa: N802 (Qt naming)
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            self.radio.rx2_freq_changed.disconnect(
                self._on_rx2_freq_changed
            )
        except Exception:
            pass
        # Stop filling the IQ ring buffer -- saves RX-loop CPU on
        # the 5053 dgrams/sec nddc=4 cadence when nobody is watching.
        try:
            self.radio._rx2_bench_active = False  # noqa: SLF001
        except Exception:
            pass
        super().closeEvent(event)
