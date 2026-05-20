// Lyra — HPSDR Protocol 1 stream (EP6 receive path).
//
// Step 2a scope: open the stream to a discovered HL2 / HL2+, run the
// real-time EP6 receive loop on its OWN dedicated OS thread
// (std::jthread — NOT the Qt event loop, NOT QThread), count incoming
// datagrams, verify the Metis header + both USB-frame sync triplets,
// detect sequence-number dropouts, surface stats to QML at 5 Hz.
//
// Locked architectural rule: the wire path lives on its own OS thread.
// This thread does NOTHING except recvfrom + integrity-check + atomic
// counter increment.  No Qt signals fire from the worker thread per
// packet — that would be 5000+ emits/sec which is bad practice even
// without a GIL.  Stats are surfaced via atomics polled by a QTimer
// on the main thread.  Step 2a deliberately does NOT parse IQ
// samples, run DSP, or produce audio.  Those land in later steps,
// each on its own bench-verifiable revertable commit.
//
// Wire reference (HPSDR Protocol 1, as implemented by the HL2 +
// AK4951-codec gateware "ak4951v4" variant the operator runs):
//
//   Host → radio control commands (64-byte UDP datagram to port 1024):
//     bytes [0..1] = 0xEF 0xFE (magic)
//     byte  [2]    = 0x04 (command)
//     byte  [3]    = command byte; bit 0 = start IQ (0x01),
//                    all zero = stop everything (0x00)
//     bytes [4..63] = zero padding
//
//   Radio → host data datagrams (1032 bytes from the radio's port 1024):
//     8-byte Metis header:
//       bytes [0..1] = 0xEF 0xFE
//       byte  [2]    = 0x01 (data frame)
//       byte  [3]    = 0x06 (endpoint = EP6 = RX IQ from radio)
//       bytes [4..7] = sequence number (BIG-endian uint32)
//     USB frame 1 (512 bytes at offset 8):
//       bytes [0..2] = 0x7F 0x7F 0x7F (sync)
//       bytes [3..7] = C0..C4 (radio→host status — decoded later)
//       bytes [8..511] = 504 bytes = 19 sample slots × 26 bytes
//     USB frame 2 (512 bytes at offset 520): same layout
//
// Per-USB-frame slot (26 bytes; nddc=4 — the HL2/HL2+ default):
//   bytes [0..2]   DDC0 I (24-bit signed BE)
//   bytes [3..5]   DDC0 Q
//   bytes [6..8]   DDC1 I
//   bytes [9..11]  DDC1 Q
//   bytes [12..14] DDC2 I
//   bytes [15..17] DDC2 Q
//   bytes [18..20] DDC3 I
//   bytes [21..23] DDC3 Q
//   bytes [24..25] mic sample (16-bit signed BE — populated on ak4951v4)
//
// At 48 kHz × 19 slots/frame × 2 frames/datagram = 5052.6 dg/sec
// expected on a healthy stream.  Step 2a verifies we can sustain
// that rate on a dedicated OS thread with zero loss + zero framing
// errors.

#pragma once

#include <QObject>
#include <QString>
#include <QTimer>
#include <atomic>
#include <cstdint>
#include <thread>
#include <stop_token>

namespace lyra::ipc {

class HL2Stream : public QObject {
    Q_OBJECT
    // Properties surfaced to QML for the live stats banner.
    Q_PROPERTY(bool    running          READ isRunning        NOTIFY runningChanged)
    Q_PROPERTY(QString targetIp         READ targetIp         NOTIFY runningChanged)
    Q_PROPERTY(double  datagramsPerSec  READ datagramsPerSec  NOTIFY statsChanged)
    Q_PROPERTY(qint64  totalDatagrams   READ totalDatagrams   NOTIFY statsChanged)
    Q_PROPERTY(qint64  dropouts         READ dropouts         NOTIFY statsChanged)
    Q_PROPERTY(qint64  framingErrors    READ framingErrors    NOTIFY statsChanged)

public:
    explicit HL2Stream(QObject *parent = nullptr);
    ~HL2Stream() override;

    bool    isRunning()        const { return running_.load(std::memory_order_acquire); }
    QString targetIp()         const { return targetIp_; }
    double  datagramsPerSec()  const { return dgPerSec_; }
    qint64  totalDatagrams()   const { return totalDg_.load(std::memory_order_relaxed); }
    qint64  dropouts()         const { return dropouts_.load(std::memory_order_relaxed); }
    qint64  framingErrors()    const { return framingErrors_.load(std::memory_order_relaxed); }

public slots:
    // Open the stream to the radio at `ip`.  Spins up a dedicated
    // std::jthread for the EP6 RX loop and sends the START control
    // packet.  Safe to call when already running (logs + ignores).
    void open(const QString &ip);

    // Send STOP, request the worker thread to exit, join it.
    // Safe to call when already stopped (no-op).
    void close();

signals:
    void runningChanged();
    void statsChanged();
    void logLine(QString line);

private slots:
    void onStatsTick();
    void onFatalError(QString reason);

private:
    void workerThread(std::stop_token stop, QString ip);

    std::jthread        worker_;
    std::atomic<bool>   running_{false};
    std::atomic<qint64> totalDg_{0};
    std::atomic<qint64> dropouts_{0};
    std::atomic<qint64> framingErrors_{0};
    std::atomic<qint64> windowDg_{0};
    double              dgPerSec_ = 0.0;
    QString             targetIp_;
    QTimer              statsTimer_;

    static constexpr quint16 kRadioPort   = 1024;
    static constexpr int     kMetisDgSize = 1032;  // 8 header + 2*512 USB
    static constexpr int     kStatPeriodMs = 200;  // 5 Hz UI updates
};

} // namespace lyra::ipc
