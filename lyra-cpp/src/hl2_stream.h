// Lyra — HPSDR Protocol 1 stream (EP6 receive + EP2 keepalive).
//
// Step 2a scope (shipped): open the stream, RX EP6 datagrams on a
// dedicated OS thread, verify Metis header + USB-frame sync, count
// datagrams + dropouts + framing errors.
//
// Step 2b scope (this commit): add the EP2 keepalive writer on its
// OWN dedicated OS thread.  HL2 gateware watchdog cuts EP6 after
// ~13 sec without host EP2 traffic — Step 2a surfaced this empirically
// on the operator's HL2+ bench (65 520 dg / ~13 sec then stream
// stops cold).  EP2 writer fires at 380 Hz (= 48 kHz audio sample
// rate / 126 audio samples per EP2 datagram) carrying a minimum-
// viable C&C config (192 kHz IQ, nddc=4, MOX off, duplex bit on)
// + zero audio/TX-IQ payload.  Keeps the gateware happy indefinitely.
//
// Locked architectural rule: the wire path runs on dedicated OS
// threads (one for RX, one for TX), each on a std::jthread, native
// WinSock2 socket shared between them (sendto + recvfrom are
// independently thread-safe at the OS level on separate directions
// of one socket).  No Qt event loop on the wire threads.  No GIL.
// No Python.  Anywhere.  Ever.
//
// References (read before coding — Lyra-native implementation,
// nothing copied):
//   * HL2 wiki Protocol.md — register map, EP2/EP6 layout
//   * HPSDR Protocol 1 spec (openHPSDR.org)
//   * CLAUDE.md §3.2 / §3.4 — operator-verified byte layouts
//   * CLAUDE.md §5 — original Python threading model (now C++23)
//   * CLAUDE.md §15.26 — Win32 HIGH_RESOLUTION timer pattern
//
// Wire reference summary:
//
//   Host → radio control (64-byte UDP datagram to radio:1024):
//     bytes [0..1] = 0xEF 0xFE (magic)
//     byte  [2]    = 0x04 (command)
//     byte  [3]    = command byte; 0x01 = start IQ, 0x00 = stop
//     bytes [4..63] = zero padding
//
//   Host → radio EP2 keepalive (1032-byte UDP datagram to radio:1024):
//     8-byte Metis header:
//       bytes [0..1] = 0xEF 0xFE (magic)
//       byte  [2]    = 0x01 (data frame)
//       byte  [3]    = 0x02 (endpoint = EP2 = host → radio)
//       bytes [4..7] = sequence number (BIG-endian uint32)
//     USB frame 1 (512 bytes at offset 8):
//       bytes [0..2] = 0x7F 0x7F 0x7F (sync)
//       byte  [3]    = C0 = frame address << 1 | MOX bit
//       bytes [4..7] = C1..C4 (config registers; frame-0 carries
//                      sample rate, OC pins, nddc, duplex bit)
//       bytes [8..511] = 504 bytes = 63 LRIQ tuples × 8 bytes
//                       (L audio + R audio + TX I + TX Q, all
//                        16-bit signed BE; ZERO during RX-only)
//     USB frame 2 (512 bytes at offset 520): same layout
//
//   Radio → host EP6 receive (1032-byte UDP datagram from radio:1024):
//     same Metis layout, byte [3] = 0x06; each USB frame's 504-byte
//     payload = 19 sample slots × 26 bytes (DDC0-3 I/Q + mic).
//     Parsing lands in Step 2c — Step 2b only verifies integrity.
//
// Frame-0 C1..C4 we send on every keepalive (matches the post-START
// gateware default the operator's HL2+ runs at — no state change):
//   C1 = 0x02 — sample rate bits [1:0] = 10 = 192 kHz per DDC
//   C2 = 0x00 — no open-collector outputs, no 10MHz ref override
//   C3 = 0x00 — no random, no dither, no preamp adjust
//   C4 = 0x1C — nddc=4 (bits [6:3] = 0011 = 0x18) | duplex bit
//               (bit 2 = 0x04) per CLAUDE.md §3.2 main-loop emission

#pragma once

#include <QElapsedTimer>
#include <QObject>
#include <QString>
#include <QTimer>
#include <atomic>
#include <cstdint>
#include <functional>
#include <thread>
#include <stop_token>

namespace lyra::ipc {

// Platform socket handle, opaque to consumers of this header (we do
// NOT drag winsock2.h through here).  On Windows the platform
// SOCKET type is UINT_PTR which is quintptr.  On POSIX it would be
// `int` — the implementation casts internally.
using SocketHandle = quintptr;
inline constexpr SocketHandle kInvalidSocket = ~SocketHandle{0};

class HL2Stream : public QObject {
    Q_OBJECT
    // RX direction (Step 2a)
    Q_PROPERTY(bool    running              READ isRunning            NOTIFY runningChanged)
    Q_PROPERTY(QString targetIp             READ targetIp             NOTIFY runningChanged)
    Q_PROPERTY(double  datagramsPerSec      READ datagramsPerSec      NOTIFY statsChanged)
    Q_PROPERTY(qint64  totalDatagrams       READ totalDatagrams       NOTIFY statsChanged)
    Q_PROPERTY(qint64  seqErrors            READ seqErrors            NOTIFY statsChanged)
    Q_PROPERTY(qint64  framingErrors        READ framingErrors        NOTIFY statsChanged)
    // TX direction (Step 2b — EP2 keepalive)
    Q_PROPERTY(double  txDatagramsPerSec    READ txDatagramsPerSec    NOTIFY statsChanged)
    Q_PROPERTY(qint64  txTotalDatagrams     READ txTotalDatagrams     NOTIFY statsChanged)
    Q_PROPERTY(qint64  txSendErrors         READ txSendErrors         NOTIFY statsChanged)
    // RX1 signal level (Step 2c — first real radio reception in C++)
    Q_PROPERTY(double  rx1DbFs              READ rx1DbFs              NOTIFY statsChanged)
    // RX1 (DDC0) receive frequency, Hz — tuning from C++ (Step 4)
    Q_PROPERTY(quint32 rx1FreqHz            READ rx1FreqHz            NOTIFY rx1FreqChanged)

public:
    explicit HL2Stream(QObject *parent = nullptr);
    ~HL2Stream() override;

    bool    isRunning()         const { return running_.load(std::memory_order_acquire); }
    QString targetIp()          const { return targetIp_; }
    double  datagramsPerSec()   const { return dgPerSec_; }
    qint64  totalDatagrams()    const { return totalDg_.load(std::memory_order_relaxed); }
    // seqErrors: count of EP6 datagrams whose sequence number was
    // anything other than (previous+1).  Incremented by 1 per event
    // — NOT summed by the gap magnitude — so a one-shot sequence
    // counter reset (which the HL2 gateware can do transiently)
    // shows up as "1 seq err" instead of nonsense.  Matches the
    // reference-implementation diagnostic posture; see CLAUDE.md
    // §15.x / FEATURES.md for the rationale.
    qint64  seqErrors()         const { return seqErrors_.load(std::memory_order_relaxed); }
    qint64  framingErrors()     const { return framingErrors_.load(std::memory_order_relaxed); }
    double  txDatagramsPerSec() const { return txDgPerSec_; }
    qint64  txTotalDatagrams()  const { return txTotalDg_.load(std::memory_order_relaxed); }
    qint64  txSendErrors()      const { return txSendErrors_.load(std::memory_order_relaxed); }
    // rx1DbFs — RMS magnitude of the RX1 baseband IQ stream in
    // dBFS (full scale = 1.0 magnitude after normalizing the
    // gateware's 24-bit signed samples).  Updated by the RX
    // worker thread every 50 ms (9600 samples at 192 kHz);
    // initial sentinel -200.0 means "no samples yet."
    double  rx1DbFs()           const { return rx1DbFs_.load(std::memory_order_relaxed); }
    quint32 rx1FreqHz()         const { return rx1FreqHz_.load(std::memory_order_relaxed); }

    // Step 3d: register a sink for DDC0 baseband IQ.  Called ONCE per
    // EP6 datagram from the RX worker thread with interleaved
    // (I,Q,…) doubles in [-1,1) (38 frames/datagram at 192 kHz nddc=4).
    // The sink runs SYNCHRONOUSLY on the RX worker thread — it is a
    // plain std::function, NOT a Qt signal, so there is no cross-thread
    // queueing on the hot path (the DSP engine consumes it inline,
    // mirroring how Thetis's cmaster pump calls fexchange0).  Must be
    // set before open() spawns the worker; not changed while running.
    void setIqSink(std::function<void(const double *, int)> sink) {
        iqSink_ = std::move(sink);
    }

public slots:
    // Open the stream to the radio at `ip`.  Creates one native UDP
    // socket, spawns the EP6 RX std::jthread + the EP2 TX std::jthread
    // (both share the socket), the TX thread sends START on entry.
    // Safe to call when already running (logs + ignores).
    void open(const QString &ip);

    // Request stop on both worker threads, join them, send STOP
    // from the main thread (best-effort), close the socket.  Safe
    // to call when already stopped (no-op).
    void close();

    // Set the DDC0 (RX1) receive frequency in Hz.  Stored in an atomic
    // the EP2 writer reads each send; takes effect on the next
    // keepalive (~2.6 ms) via the addr-2 C&C frame.  The duplex bit
    // (frame-0 C4=0x1C, sent every datagram) is what lets the gateware
    // apply post-priming RX-freq updates (CLAUDE.md §3.2).  Thread-safe.
    void setRx1FreqHz(quint32 hz);

signals:
    void runningChanged();
    void statsChanged();
    void rx1FreqChanged();
    void logLine(QString line);

private slots:
    void onStatsTick();
    void onFatalError(QString reason);

private:
    void rxWorkerLoop(std::stop_token stop, SocketHandle sock);
    void txWorkerLoop(std::stop_token stop, SocketHandle sock,
                      QString ip);

    SocketHandle         socket_ = kInvalidSocket;
    std::jthread         rxWorker_;
    std::jthread         txWorker_;
    std::atomic<bool>    running_{false};
    std::atomic<qint64>  totalDg_{0};
    std::atomic<qint64>  seqErrors_{0};
    std::atomic<qint64>  framingErrors_{0};
    std::atomic<qint64>  windowDg_{0};
    std::atomic<qint64>  txTotalDg_{0};
    std::atomic<qint64>  txWindowDg_{0};
    std::atomic<qint64>  txSendErrors_{0};
    std::atomic<quint32> txSeq_{0};
    // Step 2c: RX1 dBFS, written by the RX worker thread.  Sentinel
    // -200.0 = "no samples yet" / pre-first-window.  std::atomic<double>
    // is lock-free on x86_64 so the main-thread read in the Q_PROPERTY
    // getter doesn't take a lock.
    std::atomic<double>  rx1DbFs_{-200.0};
    // Step 4: DDC0 (RX1) receive frequency, Hz.  Read by the EP2 writer
    // each send.  Default 7.074 MHz (40m FT8) so first launch lands on
    // a known-active spot.  std::atomic<quint32> is lock-free on x86_64.
    std::atomic<quint32> rx1FreqHz_{7074000};
    // Step 3d: DDC0 IQ sink (DSP engine).  Set once before open();
    // read on the RX worker thread.  Default-empty = no DSP wired.
    std::function<void(const double *, int)> iqSink_;
    double               dgPerSec_   = 0.0;
    double               txDgPerSec_ = 0.0;
    QString              targetIp_;
    QTimer               statsTimer_;
    // Measures the ACTUAL interval between stats ticks so dg/s is
    // correct even when the 5 Hz timer fires late (e.g. main-thread
    // paint load) — dividing by a fixed period inflated the reading
    // and made the WIRE-OK band flap.
    QElapsedTimer        statsClock_;

    static constexpr quint16 kRadioPort    = 1024;
    static constexpr int     kMetisDgSize  = 1032;   // 8 hdr + 2*512 USB
    static constexpr int     kStatPeriodMs = 200;    // 5 Hz UI updates
    // EP2 keepalive cadence: 48000 audio samples/sec ÷ 126 samples
    // per datagram (63 LRIQ tuples × 2 USB frames) = 380.95 dg/sec
    // → 2.6316 ms period.  Same cadence Thetis/pihpsdr/Quisk fire.
    static constexpr int     kEp2RateHz    = 380;
};

} // namespace lyra::ipc
