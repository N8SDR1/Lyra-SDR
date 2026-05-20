// Lyra — HPSDR Protocol 1 stream implementation (EP6 receive path).
// See hl2_stream.h for the protocol reference + Step 2a scope.

#include "hl2_stream.h"

#include <QUdpSocket>
#include <QHostAddress>
#include <QByteArray>
#include <QMetaObject>
#include <Qt>

namespace lyra::ipc {

namespace {

// Build the 64-byte host→radio control packet.  start=true sends
// 0xEFFE 0x04 0x01 (start IQ); start=false sends 0xEFFE 0x04 0x00
// (stop everything).  Remainder is zero padding per HPSDR P1.
QByteArray buildControlPacket(bool start) {
    QByteArray pkt(64, char{0});
    pkt[0] = static_cast<char>(0xEF);
    pkt[1] = static_cast<char>(0xFE);
    pkt[2] = static_cast<char>(0x04);
    pkt[3] = static_cast<char>(start ? 0x01 : 0x00);
    return pkt;
}

} // namespace

HL2Stream::HL2Stream(QObject *parent) : QObject(parent) {
    statsTimer_.setInterval(kStatPeriodMs);
    connect(&statsTimer_, &QTimer::timeout,
            this, &HL2Stream::onStatsTick);
}

HL2Stream::~HL2Stream() {
    // RAII: ensure the worker thread is stopped + joined before
    // the QObject + atomics destruct.  close() is idempotent.
    close();
}

void HL2Stream::open(const QString &ip) {
    if (running_.load(std::memory_order_acquire)) {
        emit logLine(QStringLiteral("open: stream already running, ignored"));
        return;
    }
    // Defensive: if a previous worker exited on its own (e.g. fatal
    // error) and we never close()d, the jthread may still be joinable.
    // Join it before reassigning (std::jthread move-assign would
    // terminate() otherwise).
    if (worker_.joinable()) {
        worker_.request_stop();
        worker_.join();
    }

    targetIp_ = ip;
    totalDg_.store(0, std::memory_order_relaxed);
    dropouts_.store(0, std::memory_order_relaxed);
    framingErrors_.store(0, std::memory_order_relaxed);
    windowDg_.store(0, std::memory_order_relaxed);
    dgPerSec_ = 0.0;
    running_.store(true, std::memory_order_release);
    emit runningChanged();
    emit statsChanged();
    emit logLine(QStringLiteral("opening EP6 stream to %1:%2 ...")
                 .arg(ip).arg(kRadioPort));
    statsTimer_.start();

    worker_ = std::jthread([this, ip](std::stop_token stop) {
        workerThread(std::move(stop), ip);
    });
}

void HL2Stream::close() {
    if (!running_.load(std::memory_order_acquire) && !worker_.joinable()) {
        return;
    }
    emit logLine(QStringLiteral("closing EP6 stream ..."));
    if (worker_.joinable()) {
        worker_.request_stop();
        worker_.join();
    }
    statsTimer_.stop();
    // Final stats tick — flush the current window into the UI so the
    // operator sees the final count, not a stale 0.
    onStatsTick();
    running_.store(false, std::memory_order_release);
    emit runningChanged();
    emit logLine(QStringLiteral(
        "stream closed: %1 datagrams, %2 dropouts, %3 framing errors")
        .arg(totalDg_.load())
        .arg(dropouts_.load())
        .arg(framingErrors_.load()));
}

void HL2Stream::onStatsTick() {
    // Main-thread polling of the atomics the worker thread updates.
    // 5 Hz is plenty for the operator UI; per-packet signal emits
    // would be 5000+/sec which is bad practice (the GIL is gone but
    // signal/slot machinery still has overhead).
    const qint64 windowCount =
        windowDg_.exchange(0, std::memory_order_acq_rel);
    // Window is kStatPeriodMs (=200) ms long.
    dgPerSec_ = static_cast<double>(windowCount) *
                (1000.0 / static_cast<double>(kStatPeriodMs));
    emit statsChanged();
}

void HL2Stream::onFatalError(QString reason) {
    // Worker thread asked us (via QueuedConnection) to tear down due
    // to an unrecoverable error.  Run the normal close() path so the
    // UI returns to a clean idle state.
    emit logLine(QStringLiteral("FATAL: %1").arg(reason));
    close();
}

void HL2Stream::workerThread(std::stop_token stop, QString ip) {
    // Dedicated OS thread for the EP6 RX path.  No Qt event loop on
    // this thread — we use QUdpSocket synchronously with
    // waitForReadyRead() as a blocking-with-timeout primitive, then
    // drain all queued datagrams in a tight inner loop.  Diagnostic
    // signals back to the main thread go via QueuedConnection so
    // they cross the thread boundary safely.

    QUdpSocket sock;
    // Bind to all IPv4 interfaces, ephemeral port.  The OS routing
    // table picks the correct NIC to reach the radio's IP — for a
    // multi-NIC host (operator's typical setup) this is what gets
    // the EP6 reply landing on the same NIC the START went out on.
    if (!sock.bind(QHostAddress::AnyIPv4, 0,
                   QAbstractSocket::DefaultForPlatform)) {
        const QString err = sock.errorString();
        QMetaObject::invokeMethod(this, [this, err]() {
            onFatalError(QStringLiteral("bind: %1").arg(err));
        }, Qt::QueuedConnection);
        return;
    }
    const quint16 localPort = sock.localPort();
    QMetaObject::invokeMethod(this, [this, localPort]() {
        emit logLine(QStringLiteral("  bound local UDP port %1")
                     .arg(localPort));
    }, Qt::QueuedConnection);

    const QHostAddress radioAddr(ip);
    const QByteArray startPkt = buildControlPacket(true);
    const QByteArray stopPkt  = buildControlPacket(false);

    // Send START.  HPSDR P1 requires no handshake — the radio starts
    // streaming EP6 datagrams within ~10 ms of receiving this.
    const qint64 sent = sock.writeDatagram(startPkt, radioAddr, kRadioPort);
    if (sent != startPkt.size()) {
        const QString err = sock.errorString();
        QMetaObject::invokeMethod(this, [this, err]() {
            onFatalError(QStringLiteral("START send: %1").arg(err));
        }, Qt::QueuedConnection);
        sock.close();
        return;
    }
    QMetaObject::invokeMethod(this, [this]() {
        emit logLine(QStringLiteral(
            "  START sent (0xEFFE 0x04 0x01 + 60 zeros), "
            "awaiting EP6 datagrams ..."));
    }, Qt::QueuedConnection);

    // Tight recvfrom loop.
    quint32 expectedSeq = 0;
    bool    firstPacket = true;
    QByteArray buf;
    buf.resize(2048);  // generous; Metis EP6 datagrams are 1032 bytes

    while (!stop.stop_requested()) {
        // 200 ms wait: long enough that we're not spinning when the
        // stream is healthy (waitForReadyRead returns immediately
        // when a datagram is queued), short enough that stop-token
        // checks have sub-second latency for clean shutdown.
        if (!sock.waitForReadyRead(200)) {
            continue;
        }
        // Drain ALL queued datagrams before sleeping again — keeps
        // the kernel UDP receive buffer from backing up at the
        // ~5052 dg/sec wire rate.
        while (sock.hasPendingDatagrams() && !stop.stop_requested()) {
            const qint64 n =
                sock.readDatagram(buf.data(), buf.size());
            if (n < 0) continue;
            if (n != kMetisDgSize) {
                framingErrors_.fetch_add(1, std::memory_order_relaxed);
                continue;
            }
            const auto *u =
                reinterpret_cast<const std::uint8_t*>(buf.constData());

            // Metis header: 0xEFFE 0x01 0x06 + 4-byte BE seq
            if (u[0] != 0xEF || u[1] != 0xFE ||
                u[2] != 0x01 || u[3] != 0x06) {
                framingErrors_.fetch_add(1, std::memory_order_relaxed);
                continue;
            }
            const quint32 seq =
                (static_cast<quint32>(u[4]) << 24) |
                (static_cast<quint32>(u[5]) << 16) |
                (static_cast<quint32>(u[6]) <<  8) |
                 static_cast<quint32>(u[7]);
            if (firstPacket) {
                expectedSeq = seq + 1;
                firstPacket = false;
            } else {
                if (seq != expectedSeq) {
                    // Gap.  Unsigned subtraction wraps cleanly, so
                    // this counts forward jumps correctly even
                    // across the 2^32 boundary (which we won't hit
                    // in any realistic session — 2^32 / 5052 ≈ 10
                    // days continuous streaming — but be correct).
                    const quint32 gap = seq - expectedSeq;
                    dropouts_.fetch_add(gap, std::memory_order_relaxed);
                }
                expectedSeq = seq + 1;
            }

            // Both USB frames must begin with the 0x7F 0x7F 0x7F
            // sync triplet.  This is a structural integrity check —
            // if it ever fails on a real radio we have either bit
            // corruption on the wire (rare) or a parser bug.
            if (u[ 8] != 0x7F || u[ 9] != 0x7F || u[ 10] != 0x7F ||
                u[520] != 0x7F || u[521] != 0x7F || u[522] != 0x7F) {
                framingErrors_.fetch_add(1, std::memory_order_relaxed);
                continue;
            }

            totalDg_.fetch_add(1, std::memory_order_relaxed);
            windowDg_.fetch_add(1, std::memory_order_relaxed);
        }
    }

    // Send STOP — best-effort.  The radio's gateware watchdog will
    // also stop streaming if it doesn't see traffic for a few
    // seconds, but a clean STOP returns it to idle immediately so
    // the next open() doesn't see lingering EP6 traffic.
    sock.writeDatagram(stopPkt, radioAddr, kRadioPort);
    sock.close();
}

} // namespace lyra::ipc
