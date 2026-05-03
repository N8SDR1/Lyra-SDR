"""TCI (Transceiver Control Interface) v1.9 / v2.0 WebSocket server.

The TCI protocol was created and is maintained by EESDR Expert
Electronics as an open specification for SDR transceiver control.
Lyra implements it server-side so external software (WSJT-X, log4OM,
N1MM+, MixW, JS8Call, RCKRtty, FLDIGI, etc.) can drive frequency, mode,
filters, PTT, receive RX audio, and receive spectrum / IQ data over
WebSocket.

The server binds to the central Radio controller:
- Radio state changes → broadcast to all connected clients
- Inbound TCI commands → call Radio setters
- Inbound audio-stream commands → enable per-client binary streaming
- Audio + IQ data from Radio → encode + push to subscribed clients
- On new connection → send initialization commands + current state

Uses Qt's built-in QWebSocketServer (no extra dependencies) so signal
plumbing stays native.

v0.0.9.1 added binary audio + IQ streaming per TCI v2.0 spec §3.4.
The Stream struct (32-byte header + samples payload) is packed via
``struct`` and sent on the same WebSocket as text commands -- the
WS protocol distinguishes by frame type (text vs binary).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress

try:
    from PySide6.QtWebSockets import QWebSocketServer, QWebSocket
except ImportError:  # pragma: no cover
    QWebSocketServer = None
    QWebSocket = None

import numpy as np

from lyra.ham.dxcc import DxccLookup


# ── TCI binary stream structure (per TCI Protocol v2.0 §3.4) ──────────
# Header is 64 bytes (8 main uint32 fields + 8 reserved uint32 fields,
# all little-endian), followed by sample bytes.  Total frame can be up
# to ~16 KB of audio + 64-byte header.

# Stream type enumeration (TCI v2.0 §3.4)
STREAM_TYPE_IQ          = 0   # full-bandwidth IQ stream
STREAM_TYPE_RX_AUDIO    = 1   # demodulated RX audio
STREAM_TYPE_TX_AUDIO    = 2   # client → server audio for TX
STREAM_TYPE_TX_CHRONO   = 3   # TX timing markers
STREAM_TYPE_LINEOUT     = 4   # line-out audio (= RX_AUDIO for Lyra)

# Sample format identifiers
SAMPLE_FORMAT_INT16     = 0
SAMPLE_FORMAT_INT24     = 1
SAMPLE_FORMAT_INT32     = 2
SAMPLE_FORMAT_FLOAT32   = 3

_FORMAT_NAME_TO_ID = {
    "int16":   SAMPLE_FORMAT_INT16,
    "int24":   SAMPLE_FORMAT_INT24,
    "int32":   SAMPLE_FORMAT_INT32,
    "float32": SAMPLE_FORMAT_FLOAT32,
}
_FORMAT_ID_TO_NAME = {v: k for k, v in _FORMAT_NAME_TO_ID.items()}


def _pack_stream_header(
    receiver: int, sample_rate: int, sample_format: int,
    length: int, stream_type: int, channels: int,
) -> bytes:
    """Pack the 64-byte TCI Stream header (v2.0 spec §3.4).

    Layout (all little-endian uint32):
        [0]  receiver         (RX index, 0 for RX1)
        [1]  sample_rate
        [2]  format           (SAMPLE_FORMAT_*)
        [3]  codec            (0 = uncompressed, the only value defined)
        [4]  crc              (0 = unused)
        [5]  length           (sample count in payload)
        [6]  type             (STREAM_TYPE_*)
        [7]  channels         (1 or 2)
        [8..15] reserved      (8 × uint32 of zeros)
    """
    return struct.pack(
        "<IIIIIIII8I",
        int(receiver) & 0xFFFFFFFF,
        int(sample_rate) & 0xFFFFFFFF,
        int(sample_format) & 0xFFFFFFFF,
        0,  # codec
        0,  # crc
        int(length) & 0xFFFFFFFF,
        int(stream_type) & 0xFFFFFFFF,
        int(channels) & 0xFFFFFFFF,
        0, 0, 0, 0, 0, 0, 0, 0,  # reserved
    )


def _samples_to_bytes(
    samples: np.ndarray, sample_format: int, channels: int,
) -> bytes:
    """Convert a float32 audio block to TCI binary payload bytes.

    ``samples`` is mono float32 in [-1, 1].  When ``channels == 2``,
    the mono signal is duplicated to L=R (operator can't distinguish
    L from R for an SDR demod -- the demod is mono).  Format
    conversion clips to the format's range.

    Returns the packed sample bytes (no header).
    """
    if samples.size == 0:
        return b""
    a = np.asarray(samples, dtype=np.float32).reshape(-1)
    # Clip to [-1, 1] before format conversion so int formats don't
    # wrap on the rare over-target sample.
    np.clip(a, -1.0, 1.0, out=a)
    if channels == 2:
        # Duplicate mono → L,R interleaved (L0,R0,L1,R1,...)
        a = np.repeat(a, 2)
    if sample_format == SAMPLE_FORMAT_FLOAT32:
        return a.tobytes()
    if sample_format == SAMPLE_FORMAT_INT16:
        return (a * 32767.0).astype("<i2").tobytes()
    if sample_format == SAMPLE_FORMAT_INT32:
        return (a * 2147483647.0).astype("<i4").tobytes()
    if sample_format == SAMPLE_FORMAT_INT24:
        # 24-bit signed packed little-endian, 3 bytes per sample.
        i32 = (a * 8388607.0).astype(np.int32)
        # Take low 3 bytes from each int32 (little-endian).
        b = i32.astype("<i4").tobytes()
        # Reshape to (N, 4) and drop high byte.
        arr = np.frombuffer(b, dtype=np.uint8).reshape(-1, 4)
        return arr[:, :3].tobytes()
    # Unknown format -- fall back to float32 to avoid producing zero.
    return a.tobytes()


def _iq_to_bytes(
    iq: np.ndarray, sample_format: int,
) -> bytes:
    """Convert a complex64 IQ block to TCI binary payload bytes.

    IQ streams are always 2 channels (I and Q) -- the spec's
    AUDIO_STREAM_CHANNELS doesn't apply to IQ.  Layout: I0, Q0, I1,
    Q1, ... format determined by ``sample_format``.

    Returns the packed sample bytes (no header).
    """
    if iq.size == 0:
        return b""
    # Interleave I, Q as a single float32 array twice the length.
    interleaved = np.empty(iq.size * 2, dtype=np.float32)
    interleaved[0::2] = iq.real.astype(np.float32, copy=False)
    interleaved[1::2] = iq.imag.astype(np.float32, copy=False)
    np.clip(interleaved, -1.0, 1.0, out=interleaved)
    if sample_format == SAMPLE_FORMAT_FLOAT32:
        return interleaved.tobytes()
    if sample_format == SAMPLE_FORMAT_INT16:
        return (interleaved * 32767.0).astype("<i2").tobytes()
    if sample_format == SAMPLE_FORMAT_INT32:
        return (interleaved * 2147483647.0).astype("<i4").tobytes()
    if sample_format == SAMPLE_FORMAT_INT24:
        i32 = (interleaved * 8388607.0).astype(np.int32)
        b = i32.astype("<i4").tobytes()
        arr = np.frombuffer(b, dtype=np.uint8).reshape(-1, 4)
        return arr[:, :3].tobytes()
    return interleaved.tobytes()


@dataclass
class _ClientStreamState:
    """Per-client subscription state for binary audio + IQ streams.

    A TCI client connects, sends optional config commands
    (AUDIO_SAMPLERATE / AUDIO_STREAM_SAMPLE_TYPE / AUDIO_STREAM_CHANNELS
    / IQ_SAMPLERATE), then sends AUDIO_START or IQ_START to begin
    receiving binary frames.  Each client gets its own state so the
    server can satisfy heterogeneous subscribers (e.g. WSJT-X wants
    48 k mono int16 audio, simultaneously a panorama tool wants
    192 k stereo float32 IQ).

    Defaults match the TCI v2.0 spec.
    """
    audio_enabled: bool = False
    iq_enabled: bool = False
    audio_sample_rate: int = 48000      # 8 / 12 / 24 / 48 kHz per spec
    audio_format: int = SAMPLE_FORMAT_FLOAT32
    audio_channels: int = 2             # 1 or 2 per spec
    iq_sample_rate: int = 48000         # 48 / 96 / 192 / 384 kHz per spec
    iq_format: int = SAMPLE_FORMAT_FLOAT32


# Project-root-relative path to the DXCC database. If missing, flag
# lookup is silently disabled.
_CTY_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cty.dat"
_dxcc_lookup = DxccLookup(_CTY_PATH)


# Standard TCI server port for HPSDR-family rigs: 50001. (The TCI
# spec also defines 40001 as the EESDR-native default; 50001 is what
# log4OM, WSJT-X, and most other clients ship with as the default
# TCI port for non-EESDR transceivers.)
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

        # ── TCI audio + IQ streaming master toggles ──────────────────
        # Operator-facing master enables for binary streaming.  Default
        # ON so out-of-the-box behaviour matches what TCI clients
        # expect (WSJT-X / JS8Call / FLDIGI rely on AUDIO_START
        # producing audio).  Operators can disable for CPU / safety
        # reasons via the Settings dialog.
        self.allow_audio_streaming: bool = True
        self.allow_iq_streaming: bool = True
        # "Always stream" auto-starts the corresponding stream on
        # client connect, without waiting for the AUDIO_START /
        # IQ_START command.  Useful for legacy apps that connect and
        # expect immediate audio.  Default OFF (spec-compliant
        # behaviour: wait for the explicit start command).
        self.always_stream_audio: bool = False
        self.always_stream_iq: bool = False

        self._server: QWebSocketServer | None = None
        self._clients: List[QWebSocket] = []
        # Per-client streaming state (audio + IQ subscription config).
        # Keyed by the QWebSocket object identity since QWebSocket
        # itself is the unique handle for a connection.
        self._client_state: dict[int, _ClientStreamState] = {}
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
            # Initialize per-client streaming state (defaults from
            # _ClientStreamState dataclass: 48 kHz float32 stereo,
            # both audio + IQ disabled until AUDIO_START / IQ_START).
            self._client_state[id(ws)] = _ClientStreamState()
            # If the operator has enabled "always stream" toggles,
            # auto-subscribe new clients to the corresponding stream.
            if self.always_stream_audio and self.allow_audio_streaming:
                self._client_state[id(ws)].audio_enabled = True
            if self.always_stream_iq and self.allow_iq_streaming:
                self._client_state[id(ws)].iq_enabled = True
            self._send_init(ws)
            self.client_count_changed.emit(len(self._clients))

    def _on_disconnect(self, ws):
        # Drop per-client streaming state on disconnect so we don't
        # leak references and so the streaming-counts stay accurate.
        self._client_state.pop(id(ws), None)
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

        # ── Binary stream subscription commands (TCI v2.0 §3.4) ──────
        # These don't touch Radio state; they set per-client flags
        # that the audio / IQ broadcast methods consult.
        elif cmd == "AUDIO_START":
            if self.allow_audio_streaming:
                self._client_state[id(ws)].audio_enabled = True
        elif cmd == "AUDIO_STOP":
            self._client_state[id(ws)].audio_enabled = False
        elif cmd == "AUDIO_SAMPLERATE":
            # Spec: 8 / 12 / 24 / 48 kHz.  In practice operators always
            # see 48 kHz from clients; we accept the others for spec
            # compliance but the rare client requesting a non-48 rate
            # gets resampled audio (scipy resample_poly).
            if args:
                try:
                    rate = int(args[0])
                    if rate in (8000, 12000, 24000, 48000):
                        self._client_state[id(ws)].audio_sample_rate = rate
                except ValueError:
                    pass
        elif cmd == "AUDIO_STREAM_SAMPLE_TYPE":
            if args:
                fmt = args[0].strip().lower()
                if fmt in _FORMAT_NAME_TO_ID:
                    self._client_state[id(ws)].audio_format = (
                        _FORMAT_NAME_TO_ID[fmt])
        elif cmd == "AUDIO_STREAM_CHANNELS":
            if args:
                try:
                    ch = int(args[0])
                    if ch in (1, 2):
                        self._client_state[id(ws)].audio_channels = ch
                except ValueError:
                    pass
        elif cmd == "IQ_START":
            if self.allow_iq_streaming:
                self._client_state[id(ws)].iq_enabled = True
        elif cmd == "IQ_STOP":
            self._client_state[id(ws)].iq_enabled = False
        elif cmd == "IQ_SAMPLERATE":
            # Spec: 48 / 96 / 192 / 384 kHz.  We pass through Lyra's
            # current native IQ rate without resampling (a client
            # requesting a different rate gets nearest-rate fallback;
            # most TCI panorama clients accept whatever the server
            # sends).
            if args:
                try:
                    rate = int(args[0])
                    if rate in (48000, 96000, 192000, 384000):
                        self._client_state[id(ws)].iq_sample_rate = rate
                except ValueError:
                    pass
        elif cmd == "LINE_OUT_START":
            # Lyra has no separate line-out concept (the AK4951
            # codec output IS the line-out for HL2+).  Alias to
            # RX_AUDIO so legacy clients that use LINE_OUT_*
            # commands still work.
            if self.allow_audio_streaming:
                self._client_state[id(ws)].audio_enabled = True
        elif cmd == "LINE_OUT_STOP":
            self._client_state[id(ws)].audio_enabled = False

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
        # configured rate_limit_hz for the same command type. TCI clients
        # generally implement the same protection to stop flooding.
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

    # ── Binary stream broadcast (TCI v2.0 §3.4) ──────────────────────
    # Called from Radio when audio + IQ blocks are produced.  Each
    # call iterates subscribed clients and sends a binary frame
    # (header + samples) to each one matching their per-client format
    # / channels / rate config.  Cheap when no clients subscribe (one
    # dict lookup per client + early-return).

    def broadcast_audio(self, audio: np.ndarray) -> None:
        """Push a 48 kHz mono float32 audio block to every subscribed
        TCI client as a TCI binary RX_AUDIO_STREAM frame.

        Cheap when nobody is listening: walks the client list and
        skips any client with audio_enabled=False (default for newly
        connected clients until they send AUDIO_START).

        Resampling: when a client requests a non-48 kHz audio rate
        (rare -- 90+% of TCI clients use 48 kHz), the block is
        resampled with scipy.signal.resample_poly per-client.
        """
        if not self.allow_audio_streaming or audio.size == 0:
            return
        # Quick walk: are any clients actually subscribed?  Avoids
        # building format-converted byte buffers when no one is
        # listening.
        subscribed = [
            ws for ws in self._clients
            if self._client_state.get(id(ws), _ClientStreamState()
                                      ).audio_enabled
        ]
        if not subscribed:
            return
        # Group clients by (rate, format, channels) so we encode each
        # variant only once.  Most operators have all clients on the
        # default 48 kHz / int16 / mono so this is usually a 1-element
        # group.
        groups: dict[tuple[int, int, int], list[QWebSocket]] = {}
        for ws in subscribed:
            st = self._client_state[id(ws)]
            key = (st.audio_sample_rate, st.audio_format, st.audio_channels)
            groups.setdefault(key, []).append(ws)
        for (rate, fmt, channels), ws_list in groups.items():
            block = audio
            if rate != 48000:
                # Resample to client-requested rate.  Only fires for
                # the rare non-48 kHz client.
                try:
                    from scipy.signal import resample_poly
                    block = resample_poly(audio, rate, 48000).astype(
                        np.float32, copy=False)
                except Exception:
                    block = audio  # fallback: send 48 k anyway
            sample_bytes = _samples_to_bytes(block, fmt, channels)
            header = _pack_stream_header(
                receiver=0,
                sample_rate=rate,
                sample_format=fmt,
                length=block.size,    # samples per channel
                stream_type=STREAM_TYPE_RX_AUDIO,
                channels=channels,
            )
            frame = header + sample_bytes
            for ws in ws_list:
                try:
                    ws.sendBinaryMessage(frame)
                except Exception:
                    pass

    def broadcast_iq(self, iq: np.ndarray, sample_rate: int) -> None:
        """Push a complex64 IQ block to every subscribed TCI client as
        a TCI binary IQ_STREAM frame.

        IQ is always 2-channel (I and Q interleaved).  ``sample_rate``
        is Lyra's current native IQ rate (48/96/192/384 kHz from the
        operator's Rate combo).  Clients that requested a different
        rate get the native rate anyway -- IQ resampling would be
        DSP-expensive and most TCI panorama clients accept whatever
        the server sends.
        """
        if not self.allow_iq_streaming or iq.size == 0:
            return
        subscribed = [
            ws for ws in self._clients
            if self._client_state.get(id(ws), _ClientStreamState()
                                      ).iq_enabled
        ]
        if not subscribed:
            return
        # Group by format only (rate is server-side, channels=2 fixed).
        groups: dict[int, list[QWebSocket]] = {}
        for ws in subscribed:
            st = self._client_state[id(ws)]
            groups.setdefault(st.iq_format, []).append(ws)
        for fmt, ws_list in groups.items():
            sample_bytes = _iq_to_bytes(iq, fmt)
            header = _pack_stream_header(
                receiver=0,
                sample_rate=sample_rate,
                sample_format=fmt,
                length=iq.size,       # complex sample count
                stream_type=STREAM_TYPE_IQ,
                channels=2,
            )
            frame = header + sample_bytes
            for ws in ws_list:
                try:
                    ws.sendBinaryMessage(frame)
                except Exception:
                    pass

    def streaming_clients_summary(self) -> List[dict]:
        """Operator-facing diagnostic: list of currently-streaming
        clients with their config.  Used by Settings UI to show
        "WSJT-X is streaming audio at 48k int16 mono" etc.
        """
        out = []
        for ws in self._clients:
            st = self._client_state.get(id(ws))
            if st is None:
                continue
            try:
                addr = f"{ws.peerAddress().toString()}:{ws.peerPort()}"
            except Exception:
                addr = "?"
            out.append({
                "address": addr,
                "audio_enabled": st.audio_enabled,
                "audio_sample_rate": st.audio_sample_rate,
                "audio_format": _FORMAT_ID_TO_NAME.get(
                    st.audio_format, "?"),
                "audio_channels": st.audio_channels,
                "iq_enabled": st.iq_enabled,
                "iq_sample_rate": st.iq_sample_rate,
                "iq_format": _FORMAT_ID_TO_NAME.get(st.iq_format, "?"),
            })
        return out

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
