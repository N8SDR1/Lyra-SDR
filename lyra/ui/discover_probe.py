"""Network Discovery Probe — diagnose why an HL2 isn't being found.

Common discovery failure modes (and what the probe shows):

  1. Multi-NIC laptop: Wi-Fi + Ethernet, HL2 on the wrong interface.
     → Probe lists EVERY local IPv4 interface so the operator can see
       which subnet they're on vs. where the HL2 is plugged in.
     → discover() now broadcasts on every interface (fixed in
       lyra/protocol/discovery.py); the probe still surfaces the
       interface list because mismatched-subnet is the operator's
       fix, not ours.

  2. Windows Firewall blocking inbound UDP 1024.
     → Probe shows zero replies after broadcast → operator knows to
       check Defender / antivirus inbound rules.

  3. HL2 on a separate VLAN / not actually powered on.
     → Same symptom as firewall (no replies); operator can rule it
       out with the unicast probe (enter HL2 IP directly → if it
       still doesn't reply, the HL2 itself isn't reachable).

  4. HL2 already busy with another HPSDR-protocol SDR client.
     → Probe shows reply with status byte 0x03 (busy) instead of
       0x02 (idle); the parsed entry will say "busy=True".

  5. Wrong IP entered in Settings.
     → Probe lets operator try the unicast path with the IP they're
       about to use, sees if it works, before even leaving the
       dialog.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)


class _DiscoveryWorker(QThread):
    """Runs discovery on a background thread so the dialog UI stays
    responsive (broadcast wait is ~1.5 sec by default, plus
    multi-NIC iteration). Emits one of finished_ok / finished_error
    when done."""
    finished_ok = Signal(list, list)   # (RadioInfo list, debug log lines)
    finished_error = Signal(str)

    def __init__(self, target_ip: Optional[str] = None,
                 timeout_s: float = 1.5, attempts: int = 2):
        super().__init__()
        self._target_ip = target_ip
        self._timeout_s = timeout_s
        self._attempts = attempts

    def run(self):
        try:
            from lyra.protocol.discovery import discover
            log: list[str] = []
            radios = discover(
                timeout_s=self._timeout_s,
                attempts=self._attempts,
                target_ip=self._target_ip,
                debug_log=log,
            )
            self.finished_ok.emit(radios, log)
        except Exception as e:
            self.finished_error.emit(str(e))


class NetworkDiscoveryProbeDialog(QDialog):
    """Operator-facing diagnostic dialog. Lists local network
    interfaces, runs discovery (broadcast or unicast), shows the
    raw debug log + parsed results, copies-to-clipboard for sending
    to support."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Network Discovery Probe")
        self.setMinimumSize(820, 580)
        self._build_ui()
        self._refresh_interfaces()
        self._worker: Optional[_DiscoveryWorker] = None

    # ── UI ─────────────────────────────────────────────────────────
    def _build_ui(self):
        v = QVBoxLayout(self)

        intro = QLabel(
            "<b>Diagnose why your HL2 isn't being found by the "
            "▶ Start button's auto-discover.</b><br><br>"
            "1. Check the local interface list below — make sure your "
            "Lyra PC is on the SAME subnet as the HL2 (e.g. both "
            "10.10.30.x, or both 192.168.1.x).<br>"
            "2. Click <b>Run Discovery (broadcast)</b> to probe every "
            "interface. If it finds the HL2, you're set — close this "
            "and click ▶ Start.<br>"
            "3. If broadcast doesn't find it but you know the HL2's "
            "IP, type the IP and click <b>Run Discovery (unicast)</b> "
            "— that bypasses broadcast and asks that one IP directly."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)

        # ── Interface list ─────────────────────────────────────────
        v.addWidget(QLabel("<b>Local IPv4 interfaces on this PC:</b>"))
        self._iface_label = QLabel("(scanning…)")
        self._iface_label.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace; "
            "background: #11161e; padding: 6px; border: 1px solid #2a3340; "
            "border-radius: 3px;")
        self._iface_label.setWordWrap(True)
        v.addWidget(self._iface_label)
        rescan = QPushButton("Re-scan interfaces")
        rescan.setFixedWidth(160)
        rescan.clicked.connect(self._refresh_interfaces)
        v.addWidget(rescan, alignment=Qt.AlignLeft)

        # ── Discovery controls ─────────────────────────────────────
        v.addWidget(QLabel("<b>Discovery:</b>"))
        ctrl_row = QHBoxLayout()
        self._broadcast_btn = QPushButton("▶ Run Discovery (broadcast)")
        self._broadcast_btn.clicked.connect(self._run_broadcast)
        ctrl_row.addWidget(self._broadcast_btn)
        ctrl_row.addSpacing(20)
        ctrl_row.addWidget(QLabel("Or unicast to:"))
        self._unicast_ip = QLineEdit()
        self._unicast_ip.setPlaceholderText("e.g. 10.10.30.100")
        self._unicast_ip.setFixedWidth(180)
        ctrl_row.addWidget(self._unicast_ip)
        self._unicast_btn = QPushButton("Run Discovery (unicast)")
        self._unicast_btn.clicked.connect(self._run_unicast)
        ctrl_row.addWidget(self._unicast_btn)
        ctrl_row.addStretch(1)
        v.addLayout(ctrl_row)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        v.addWidget(self._progress)

        # ── Results: parsed table + raw log ─────────────────────────
        v.addWidget(QLabel("<b>Found radios:</b>"))
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["IP", "MAC", "Board", "Gateware", "Busy?"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMaximumHeight(120)
        v.addWidget(self._table)

        v.addWidget(QLabel("<b>Diagnostic log:</b>"))
        self._log = QTextBrowser()
        self._log.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 10pt;")
        log_font = QFont("Consolas")
        log_font.setPointSize(10)
        self._log.setFont(log_font)
        v.addWidget(self._log, 1)

        # ── Buttons ────────────────────────────────────────────────
        h = QHBoxLayout()
        copy_btn = QPushButton("Copy log to clipboard")
        copy_btn.clicked.connect(self._copy_log)
        h.addWidget(copy_btn)
        h.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        h.addWidget(close_btn)
        v.addLayout(h)

    # ── Actions ────────────────────────────────────────────────────
    def _refresh_interfaces(self):
        from lyra.protocol.discovery import list_local_ipv4_addresses
        ips = list_local_ipv4_addresses()
        if not ips:
            self._iface_label.setText(
                "<i>No usable IPv4 interfaces detected. Check that the "
                "PC has a network connection.</i>")
            return
        # Show each IP with a heuristic note
        lines = []
        for ip in ips:
            note = ""
            if ip.startswith("10."):
                note = "  ← typical lab / wired subnet"
            elif ip.startswith("192.168."):
                note = "  ← typical home router subnet"
            elif ip.startswith("172."):
                note = "  ← typical Docker / VM bridge"
            lines.append(f"  {ip}{note}")
        self._iface_label.setText("\n".join(lines))

    def _run_broadcast(self):
        self._start_discovery(target_ip=None, mode_label="BROADCAST (every interface)")

    def _run_unicast(self):
        ip = self._unicast_ip.text().strip()
        if not ip:
            self._log.setHtml(
                "<p style='color:#ff8c3a'>Enter an IP first (e.g. "
                "10.10.30.100) before clicking <b>Run Discovery "
                "(unicast)</b>.</p>")
            return
        self._start_discovery(target_ip=ip, mode_label=f"UNICAST to {ip}")

    def _start_discovery(self, target_ip: Optional[str], mode_label: str):
        self._broadcast_btn.setEnabled(False)
        self._unicast_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._table.setRowCount(0)
        self._log.setPlainText(f"=== Mode: {mode_label} ===\n")
        # Background worker so the UI doesn't freeze during the
        # ~1.5 sec multi-NIC sweep.
        self._worker = _DiscoveryWorker(target_ip=target_ip,
                                        timeout_s=1.5, attempts=2)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.finished_error.connect(self._on_error)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_finished(self, radios: list, log: list):
        self._progress.setVisible(False)
        self._broadcast_btn.setEnabled(True)
        self._unicast_btn.setEnabled(True)
        # Render parsed-results table
        self._table.setRowCount(len(radios))
        for i, r in enumerate(radios):
            cells = [
                r.ip, r.mac, r.board_name,
                f"v{r.code_version}.{r.beta_version}",
                "BUSY" if r.is_busy else "idle",
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(str(txt))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if col == 4 and r.is_busy:
                    from PySide6.QtGui import QColor
                    item.setForeground(QColor(255, 140, 60))
                self._table.setItem(i, col, item)
        # Append diagnostic log
        suffix = ""
        if not radios:
            suffix = (
                "\n\n=== TROUBLESHOOTING ===\n"
                "No radios found. Things to check:\n"
                "  1. HL2 powered on and Ethernet cable connected?\n"
                "  2. PC and HL2 on the SAME subnet (compare interface\n"
                "     list above with HL2 LCD or web UI IP)?\n"
                "  3. Windows Defender Firewall blocking inbound UDP\n"
                "     1024 for python.exe / Lyra.exe? Allow it.\n"
                "  4. Another HPSDR-protocol SDR client already\n"
                "     connected to the HL2? Close it first.\n"
                "  5. If you know the HL2 IP, try the unicast button —\n"
                "     bypasses broadcast entirely.\n")
        self._log.setPlainText(
            self._log.toPlainText() + "\n".join(log) + suffix)

    def _on_error(self, msg: str):
        self._progress.setVisible(False)
        self._broadcast_btn.setEnabled(True)
        self._unicast_btn.setEnabled(True)
        self._log.setHtml(
            f"<p style='color:#ff4040'>Discovery failed: {msg}</p>")

    def _copy_log(self):
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(self._log.toPlainText())
