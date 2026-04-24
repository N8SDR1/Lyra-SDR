"""TCI (Transceiver Control Interface) v1.9 WebSocket server.

Exposes Lyra to external software (WSJT-X, log4OM, N1MM+, MixW, JS8Call,
RCKRtty, etc.) via the ExpertSDR3 TCI protocol. The server binds to the
central Radio controller:
- Radio state changes → broadcast to all connected clients
- Inbound TCI commands → call Radio setters
- On new connection → send initialization commands + current state

Uses Qt's built-in QWebSocketServer (no extra dependencies) so signal
plumbing stays native.

Reference: D:/sdrprojects/TCI Protocol.pdf (v2.0, backward-compatible
with 1.9). Implements the command subset that external logging/keying
apps actually use. More commands can be added as needed.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress

try:
    from PySide6.QtWebSockets import QWebSocketServer, QWebSocket
except ImportError:  # pragma: no cover
    QWebSocketServer = None
    QWebSocket = None

from lyra.ham.dxcc import DxccLookup


# Project-root-relative path to the DXCC database. If missing, flag
# lookup is silently disabled.
_CTY_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cty.dat"
_dxcc_lookup = DxccLookup(_CTY_PATH)


# HPSDR convention: 50001. ExpertSDR3 native uses 40001. 50001 is what
# log4OM / WSJT-X default TCI configs expect.
TCI_DEFAULT_PORT = 50001


class TciServer(QObject):
    """TCI WebSocket server bound to a Radio instance."""

    running_changed = Signal(bool)
    client_count_changed = Signal(int)
    status_message = Signal(str, int)

    def __init__(self, radio, port: int = TCI_DEFAULT_PORT):
        super().__init__()
        self.radio = radio
        self.bind_host = "127.0.0.1"   # localhost by default; HPSDR convention
        self.port = port
        # User-configurable behavior (matching standard TCI options)
        self.rate_limit_hz = 50              # max outbound msgs/sec per channel
        self.send_initial_state_on_connect = True
        self.own_callsign = ""               # for TCI spot announcements
        self.log_traffic = False             # print all TCI I/O to console
        self._server: QWebSocketServer | None = None
        self._clients: List[QWebSocket] = []
        self._last_broadcast_ns: dict[str, int] = {}
        self._traffic_log: List[str] = []
        self._bind_radio_signals()

    # ── Lifecycle ─────────────────────────────────────────────────────
    def start(self) -> bool:
        if QWebSocketServer is None:
            self.status_message.emit(
                "QtWebSockets module missing — install PySide6[websockets]", 5000)
            return False
        if self._server is not None:
            return True
        self._server = QWebSocketServer("Lyra-TCI", QWebSocketServer.NonSecureMode)
        host_addr = (QHostAddress(self.bind_host)
                     if self.bind_host not in ("0.0.0.0", "", "*")
                     else QHostAddress.Any)
        if not self._server.listen(host_addr, self.port):
            err = self._server.errorString()
            self._server = None
            self.status_message.emit(f"TCI listen failed on port {self.port}: {err}", 5000)
            return False
        self._server.newConnection.connect(self._on_new_connection)
        self.status_message.emit(f"TCI server listening on port {self.port}", 4000)
        self.running_changed.emit(True)
        return True

    def stop(self):
        for ws in list(self._clients):
            try:
                ws.close()
            except Exception:
                pass
        self._clients.clear()
        self.client_count_changed.emit(0)
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        self.running_changed.emit(False)

    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ── New client handling ───────────────────────────────────────────
    def _on_new_connection(self):
        while self._server.hasPendingConnections():
            ws = self._server.nextPendingConnection()
            ws.textMessageReceived.connect(
                lambda msg, w=ws: self._on_text(w, msg))
            ws.disconnected.connect(lambda w=ws: self._on_disconnect(w))
            self._clients.append(ws)
            self._send_init(ws)
            self.client_count_changed.emit(len(self._clients))

    def _on_disconnect(self, ws):
        try:
            self._clients.remove(ws)
        except ValueError:
            pass
        ws.deleteLater()
        self.client_count_changed.emit(len(self._clients))

    def _send_init(self, ws: QWebSocket):
        """TCI requires the server to send initialization + current state
        on connect. WSJT-X/log4OM rely on this sequence."""
        r = self.radio
        modulations = ",".join(m for m in r.ALL_MODES if m not in ("Tone", "Off"))
        init = [
            "protocol:Lyra,1.9;",
            "device:HermesLite2;",
            "receive_only:false;",
            "vfo_limits:10000,55000000;",
            f"if_limits:-{r.rate // 2},{r.rate // 2};",
            "trx_count:1;",
            "channel_count:1;",
            f"modulations_list:{modulations};",
            "ready;",
        ]
        if self.send_initial_state_on_connect:
            init.extend([
                f"dds:0,{r.freq_hz};",
                f"vfo:0,0,{r.freq_hz};",
                f"modulation:0,{self._to_tci_mode(r.mode)};",
                "trx:0,false;",
                "start;" if r.is_streaming else "stop;",
            ])
        for cmd in init:
            self._send_to(ws, cmd)

    # ── Inbound command handling ──────────────────────────────────────
    def _on_text(self, ws, msg: str):
        self._record_log(f"< {msg}")
        msg = msg.strip().rstrip(";").strip()
        if not msg:
            return
        if ":" in msg:
            cmd, args_str = msg.split(":", 1)
            args = [a.strip() for a in args_str.split(",")]
        else:
            cmd, args = msg, []
        cmd = cmd.strip().upper()
        try:
            self._dispatch(ws, cmd, args)
        except Exception as e:  # noqa: BLE001
            print(f"[TCI] error handling '{msg}': {e}")

    def _dispatch(self, ws, cmd: str, args: List[str]):
        r = self.radio
        if cmd == "START":
            r.start()
        elif cmd == "STOP":
            r.stop()
        elif cmd == "DDS":
            # Read: DDS:0;    Set: DDS:0,freq;
            if len(args) >= 2:
                try:
                    r.set_freq_hz(int(args[1]))
                except ValueError:
                    pass
            elif len(args) == 1:
                ws.sendTextMessage(f"dds:{args[0]},{r.freq_hz};")
        elif cmd == "VFO":
            # Read: VFO:0,0;   Set: VFO:0,0,freq;
            if len(args) >= 3:
                try:
                    r.set_freq_hz(int(args[2]))
                except ValueError:
                    pass
            elif len(args) >= 2:
                ws.sendTextMessage(f"vfo:{args[0]},{args[1]},{r.freq_hz};")
        elif cmd == "IF":
            # IF:rx,ch,offset_hz — tune the RX passband within panorama.
            # For single-RX operation, this is the same as VFO but relative.
            # We treat it as an offset from current DDS.
            if len(args) >= 3:
                try:
                    offset = int(args[2])
                    r.set_freq_hz(r.freq_hz + offset)
                except ValueError:
                    pass
        elif cmd == "MODULATION":
            if len(args) >= 2:
                mode = self._from_tci_mode(args[1])
                if mode in r.ALL_MODES:
                    r.set_mode(mode)
            elif len(args) == 1:
                ws.sendTextMessage(f"modulation:{args[0]},{self._to_tci_mode(r.mode)};")
        elif cmd == "TRX":
            # No TX yet — acknowledge with false.
            idx = args[0] if args else "0"
            ws.sendTextMessage(f"trx:{idx},false;")
        elif cmd == "TUNE":
            idx = args[0] if args else "0"
            ws.sendTextMessage(f"tune:{idx},false;")
        elif cmd == "RIT_ENABLE":
            idx = args[0] if args else "0"
            ws.sendTextMessage(f"rit_enable:{idx},false;")
        elif cmd == "XIT_ENABLE":
            idx = args[0] if args else "0"
            ws.sendTextMessage(f"xit_enable:{idx},false;")
        elif cmd == "SPOT":
            # spot:callsign,mode,freq_hz[,argb_color];
            if len(args) >= 3:
                call = args[0]
                mode = args[1]
                try:
                    freq = int(args[2])
                except ValueError:
                    return
                argb = 0xFFFFD700  # default gold
                if len(args) >= 4:
                    try:
                        argb = int(args[3])
                    except ValueError:
                        pass
                # Look up flag from DXCC prefix and build a display label
                # (e.g., "🇺🇸 N8SDR"). The raw callsign stays as the key
                # so `spot_delete` / `spot_activated` round-trip correctly.
                display = call
                if _dxcc_lookup.is_loaded and not any(
                    0x1F1E6 <= ord(c) <= 0x1F1FF for c in call[:4]
                ):
                    display = _dxcc_lookup.enrich(call) or call
                r.add_spot(call, mode, freq, argb, display=display)
        elif cmd == "SPOT_DELETE":
            if args:
                r.delete_spot(args[0])
        elif cmd == "SPOT_CLEAR":
            r.clear_spots()
        # Unknown commands are silently ignored (per spec).

    # ── Mode name mapping ─────────────────────────────────────────────
    @staticmethod
    def _to_tci_mode(mode: str) -> str:
        """Our internal mode names → TCI mode names.
        TCI uses: AM, SAM, DSB, LSB, USB, CW, NFM, DIGL, DIGU, WFM, DRM.
        """
        return {"CWL": "CW", "CWU": "CW", "FM": "NFM"}.get(mode, mode)

    @staticmethod
    def _from_tci_mode(tci: str) -> str:
        """TCI mode names → our internal names. CW maps to CWU by default."""
        t = tci.strip().upper()
        return {"CW": "CWU", "NFM": "FM", "WFM": "FM"}.get(t, t)

    # ── Broadcasting radio state changes to all clients ───────────────
    def _bind_radio_signals(self):
        r = self.radio
        r.freq_changed.connect(self._on_freq_changed)
        r.mode_changed.connect(self._on_mode_changed)
        r.stream_state_changed.connect(self._on_stream_changed)
        r.rate_changed.connect(self._on_rate_changed)
        r.spot_activated.connect(self._on_spot_activated)

    def _on_spot_activated(self, call: str, mode: str, freq_hz: int):
        color = 0xFFFFD700
        for s in self.radio.spots:
            if s["call"] == call:
                color = s["color"]
                break
        self._broadcast(f"spot_activated:{call},{mode},{freq_hz},{color};")

    def _on_freq_changed(self, hz: int):
        self._broadcast(f"dds:0,{hz};")
        self._broadcast(f"vfo:0,0,{hz};")

    def _on_mode_changed(self, mode: str):
        self._broadcast(f"modulation:0,{self._to_tci_mode(mode)};")

    def _on_stream_changed(self, running: bool):
        self._broadcast("start;" if running else "stop;")

    def _on_rate_changed(self, rate: int):
        self._broadcast(f"if_limits:-{rate // 2},{rate // 2};")

    def _broadcast(self, msg: str):
        if not self._clients:
            return
        # Per-command rate limit: drop updates that arrive faster than the
        # configured rate_limit_hz for the same command type. some reference clients have
        # the same protection to stop flooding clients.
        import time
        key = msg.split(":", 1)[0]
        min_interval_ns = int(1e9 / max(self.rate_limit_hz, 1))
        now = time.monotonic_ns()
        last = self._last_broadcast_ns.get(key, 0)
        if now - last < min_interval_ns:
            return
        self._last_broadcast_ns[key] = now
        for ws in self._clients:
            try:
                self._send_to(ws, msg)
            except Exception:
                pass

    def _send_to(self, ws: QWebSocket, msg: str):
        ws.sendTextMessage(msg)
        self._record_log(f"> {msg}")

    def _record_log(self, line: str):
        # Always keep a bounded ring — log viewer needs recent history
        # even if the user enabled the traffic checkbox after the fact.
        self._traffic_log.append(line)
        if len(self._traffic_log) > 1000:
            self._traffic_log = self._traffic_log[-800:]
        if self.log_traffic:
            print(f"[TCI] {line}")

    @property
    def traffic_log(self) -> List[str]:
        return list(self._traffic_log)
