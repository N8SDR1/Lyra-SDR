// Lyra — HPSDR Protocol 1 stream implementation (EP6 receive + EP2
// keepalive).  See hl2_stream.h for the locked architecture +
// protocol reference.

#include "hl2_stream.h"

// WinSock2 MUST be included before windows.h to avoid winsock 1.x
// being pulled in via windows.h transitively.  NOMINMAX keeps the
// windows.h macros from clobbering std::min/std::max.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <QByteArray>
#include <QMetaObject>
#include <Qt>
#include <algorithm>
#include <cmath>
#include <cstring>

namespace lyra::ipc {

namespace {

// 64-byte HPSDR P1 host→radio control packet.  start=true sends
// 0xEFFE 0x04 0x01 (start IQ); start=false sends 0xEFFE 0x04 0x00
// (stop everything).
QByteArray buildControlPacket(bool start) {
    QByteArray pkt(64, char{0});
    pkt[0] = static_cast<char>(0xEF);
    pkt[1] = static_cast<char>(0xFE);
    pkt[2] = static_cast<char>(0x04);
    pkt[3] = static_cast<char>(start ? 0x01 : 0x00);
    return pkt;
}

// 1032-byte EP2 host→radio keepalive datagram.  Frame-0 C&C carries
// the minimum-viable HL2 config (192 kHz IQ, nddc=4, MOX off,
// duplex bit set) consistent with the post-START gateware default
// — sending this every 2.6 ms satisfies the gateware watchdog
// without re-configuring anything.  Audio out + TX I/Q bytes are
// all zero (RX-only state).
//
// The sequence-number bytes [4..7] are placeholder zeros here;
// the writer thread updates them in-place per send to avoid
// allocating a new QByteArray 380 times per second.
QByteArray buildEp2KeepaliveTemplate() {
    QByteArray pkt(1032, char{0});
    auto *u = reinterpret_cast<std::uint8_t*>(pkt.data());
    // Metis header: magic + data + endpoint=0x02 (host→radio EP2)
    u[0] = 0xEF; u[1] = 0xFE; u[2] = 0x01; u[3] = 0x02;
    // bytes [4..7] = sequence (filled per send)

    // USB frame 1 (offset 8): sync + frame-0 C&C + 504 zero data
    u[ 8] = 0x7F; u[ 9] = 0x7F; u[10] = 0x7F;
    u[11] = 0x00;   // C0 = frame address 0, MOX off
    u[12] = 0x02;   // C1 = sample rate bits [1:0] = 10 = 192 kHz
    u[13] = 0x00;   // C2 = no OC pins, no 10MHz ref override
    u[14] = 0x00;   // C3 = no random, no dither, no preamp adjust
    u[15] = 0x1C;   // C4 = nddc=4 (0x18) | duplex bit (0x04)
    // bytes [16..519] = 504 bytes audio/IQ payload = zero

    // USB frame 2 (offset 520): same minimal frame-0 C&C
    u[520] = 0x7F; u[521] = 0x7F; u[522] = 0x7F;
    u[523] = 0x00;
    u[524] = 0x02;
    u[525] = 0x00;
    u[526] = 0x00;
    u[527] = 0x1C;
    // bytes [528..1031] = 504 bytes audio/IQ payload = zero

    return pkt;
}

// Stringify a Windows error code (FormatMessage) for log lines.
QString winsockError(int code) {
    wchar_t *buf = nullptr;
    const DWORD len = ::FormatMessageW(
        FORMAT_MESSAGE_ALLOCATE_BUFFER |
        FORMAT_MESSAGE_FROM_SYSTEM     |
        FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, static_cast<DWORD>(code),
        MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
        reinterpret_cast<wchar_t*>(&buf), 0, nullptr);
    QString descr;
    if (len && buf) {
        descr = QString::fromWCharArray(buf, len).trimmed();
        ::LocalFree(buf);
    }
    return descr.isEmpty()
        ? QStringLiteral("WSA=%1").arg(code)
        : QStringLiteral("WSA=%1: %2").arg(code).arg(descr);
}

// Open a native UDP socket bound to ANY:ephemeral.  Sets a 4 MiB
// receive buffer (5 MB/sec EP6 wire rate × ~0.8 sec headroom for
// brief GUI / scheduler stalls — much larger than the kernel
// default 64 KiB which can drop packets under contention).
SocketHandle openNativeUdpSocket(QString *errOut) {
    const SOCKET s = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s == INVALID_SOCKET) {
        if (errOut) *errOut = QStringLiteral("socket: %1")
                              .arg(winsockError(::WSAGetLastError()));
        return kInvalidSocket;
    }
    int rcvbuf = 4 * 1024 * 1024;
    ::setsockopt(s, SOL_SOCKET, SO_RCVBUF,
                 reinterpret_cast<const char*>(&rcvbuf),
                 sizeof(rcvbuf));
    sockaddr_in local{};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    local.sin_port = 0;
    if (::bind(s, reinterpret_cast<sockaddr*>(&local),
               sizeof(local)) != 0) {
        if (errOut) *errOut = QStringLiteral("bind: %1")
                              .arg(winsockError(::WSAGetLastError()));
        ::closesocket(s);
        return kInvalidSocket;
    }
    return static_cast<SocketHandle>(s);
}

quint16 localPortOf(SocketHandle sh) {
    sockaddr_in local{};
    int len = sizeof(local);
    if (::getsockname(static_cast<SOCKET>(sh),
                      reinterpret_cast<sockaddr*>(&local), &len) == 0) {
        return ntohs(local.sin_port);
    }
    return 0;
}

} // namespace

HL2Stream::HL2Stream(QObject *parent) : QObject(parent) {
    statsTimer_.setInterval(kStatPeriodMs);
    connect(&statsTimer_, &QTimer::timeout,
            this, &HL2Stream::onStatsTick);
}

HL2Stream::~HL2Stream() {
    close();
}

void HL2Stream::open(const QString &ip) {
    if (running_.load(std::memory_order_acquire)) {
        emit logLine(QStringLiteral("open: stream already running, ignored"));
        return;
    }
    // Defensive: if a previous worker exited on its own (e.g. fatal
    // error path) the jthread may still be joinable.  Join before
    // reassigning so std::jthread move-assign doesn't terminate().
    if (rxWorker_.joinable()) { rxWorker_.request_stop(); rxWorker_.join(); }
    if (txWorker_.joinable()) { txWorker_.request_stop(); txWorker_.join(); }
    if (socket_ != kInvalidSocket) {
        ::closesocket(static_cast<SOCKET>(socket_));
        socket_ = kInvalidSocket;
    }

    QString err;
    socket_ = openNativeUdpSocket(&err);
    if (socket_ == kInvalidSocket) {
        emit logLine(QStringLiteral("open: %1").arg(err));
        return;
    }
    const quint16 lport = localPortOf(socket_);

    targetIp_ = ip;
    totalDg_.store(0);  seqErrors_.store(0);
    framingErrors_.store(0);  windowDg_.store(0);
    txTotalDg_.store(0);  txWindowDg_.store(0);
    txSendErrors_.store(0);  txSeq_.store(0);
    rx1DbFs_.store(-200.0, std::memory_order_relaxed);
    dgPerSec_   = 0.0;
    txDgPerSec_ = 0.0;
    running_.store(true, std::memory_order_release);
    emit runningChanged();
    emit statsChanged();
    emit logLine(QStringLiteral(
        "opening EP6 stream to %1:%2 (local port %3) ...")
        .arg(ip).arg(kRadioPort).arg(lport));

    statsTimer_.start();

    // Spawn RX first so it's already listening when TX sends START.
    // Native UDP sendto + recvfrom on one socket from different
    // threads is documented thread-safe at the OS level.
    const SocketHandle sh = socket_;
    rxWorker_ = std::jthread([this, sh](std::stop_token stop) {
        rxWorkerLoop(std::move(stop), sh);
    });
    txWorker_ = std::jthread([this, sh, ip](std::stop_token stop) {
        txWorkerLoop(std::move(stop), sh, ip);
    });
}

void HL2Stream::close() {
    if (!running_.load(std::memory_order_acquire) &&
        !rxWorker_.joinable() &&
        !txWorker_.joinable() &&
        socket_ == kInvalidSocket) {
        return;
    }
    emit logLine(QStringLiteral("closing EP6 stream ..."));

    // Request stop on both workers BEFORE joining either so they
    // wind down in parallel (RX: bounded by recv timeout 100 ms,
    // TX: bounded by waitable-timer wait cap 100 ms).
    if (rxWorker_.joinable()) rxWorker_.request_stop();
    if (txWorker_.joinable()) txWorker_.request_stop();
    if (rxWorker_.joinable()) rxWorker_.join();
    if (txWorker_.joinable()) txWorker_.join();

    // Both workers stopped — main thread sends STOP, then closes
    // the socket.  Best-effort; if STOP doesn't reach the gateware
    // its own watchdog will idle the stream within ~13 sec anyway.
    if (socket_ != kInvalidSocket) {
        sockaddr_in dest{};
        dest.sin_family = AF_INET;
        dest.sin_port   = htons(kRadioPort);
        ::inet_pton(AF_INET,
                    targetIp_.toLatin1().constData(),
                    &dest.sin_addr);
        const QByteArray stopPkt = buildControlPacket(false);
        ::sendto(static_cast<SOCKET>(socket_),
                 stopPkt.constData(), stopPkt.size(), 0,
                 reinterpret_cast<sockaddr*>(&dest), sizeof(dest));
        ::closesocket(static_cast<SOCKET>(socket_));
        socket_ = kInvalidSocket;
    }

    statsTimer_.stop();
    onStatsTick();  // flush the final window so the UI shows true totals
    running_.store(false, std::memory_order_release);
    emit runningChanged();
    emit logLine(QStringLiteral(
        "stream closed: RX %1 dg (%2 seq errs, %3 framing), "
        "TX %4 keepalives (%5 send errors)")
        .arg(totalDg_.load())
        .arg(seqErrors_.load())
        .arg(framingErrors_.load())
        .arg(txTotalDg_.load())
        .arg(txSendErrors_.load()));
}

void HL2Stream::onStatsTick() {
    const qint64 rxWin = windowDg_.exchange(0,   std::memory_order_acq_rel);
    const qint64 txWin = txWindowDg_.exchange(0, std::memory_order_acq_rel);
    const double scale = 1000.0 / static_cast<double>(kStatPeriodMs);
    dgPerSec_   = static_cast<double>(rxWin) * scale;
    txDgPerSec_ = static_cast<double>(txWin) * scale;
    emit statsChanged();
}

void HL2Stream::onFatalError(QString reason) {
    emit logLine(QStringLiteral("FATAL: %1").arg(reason));
    close();
}

// ----------------------------------------------------------------
// RX worker — dedicated OS thread, drains EP6 datagrams at line
// rate (~5040 dg/sec at 192 kHz nddc=4), verifies Metis header +
// USB sync, counts dropouts via sequence-number gaps.

void HL2Stream::rxWorkerLoop(std::stop_token stop, SocketHandle sh) {
    const SOCKET s = static_cast<SOCKET>(sh);

    // 100 ms recv timeout — bounds stop-token check latency.  The
    // healthy wire rate is ~5040 dg/sec so recvfrom returns
    // immediately almost every call; the timeout only fires during
    // the brief window between Open and the first EP6 datagram.
    DWORD timeout = 100;
    ::setsockopt(s, SOL_SOCKET, SO_RCVTIMEO,
                 reinterpret_cast<const char*>(&timeout),
                 sizeof(timeout));

    quint32 expectedSeq = 0;
    bool    firstPacket = true;
    QByteArray buf;
    buf.resize(2048);  // generous; Metis EP6 datagrams are 1032 bytes

    // ---- Step 2c: RX1 dBFS accumulator -----------------------
    // Local-to-this-thread state — re-initialized per-open since
    // each open() spawns a fresh worker.  The accumulator sums
    // magnitude² (I²+Q²) across kRmsWindowSamples DDC0 samples,
    // then computes dBFS = 10·log₁₀(meanSq) and atomically
    // publishes to rx1DbFs_ for the QML banner.  At 192 kHz IQ
    // rate, 9600 samples = 50 ms — updates 20× per second so the
    // 5 Hz QML stats tick always sees the most recent ~50 ms RMS.
    //
    // Wire format for each 26-byte slot (nddc=4, gateware default
    // verified ak4951v4 RTL + Thetis networkproto1.c:527-541):
    //   bytes [0..2]   DDC0 I — 24-bit signed BE
    //   bytes [3..5]   DDC0 Q — 24-bit signed BE
    //   bytes [6..23]  DDC1/2/3 I/Q  — Step 5 RX2 work, ignored here
    //   bytes [24..25] mic sample    — Step 3+ TX work, ignored here
    //
    // 24-bit normalization: pack the 3 bytes into the TOP 24 bits
    // of an int32 with the low byte zero, then divide by 2³¹.
    // bytes[0]'s high bit naturally lands at bit 31 = the sign
    // bit of int32, so the sign extends correctly without any
    // explicit cast.  Range: [-1.0, +1.0).
    double rmsAcc       = 0.0;
    int    rmsAccCount  = 0;
    constexpr int kRmsWindowSamples = 9600;        // 50 ms @ 192 kHz
    constexpr double kInv2Pow31      = 1.0 / 2147483648.0;

    while (!stop.stop_requested()) {
        sockaddr_in from{};
        int fromLen = sizeof(from);
        const int n = ::recvfrom(s, buf.data(), buf.size(), 0,
                                 reinterpret_cast<sockaddr*>(&from),
                                 &fromLen);
        if (n == SOCKET_ERROR) {
            const int err = ::WSAGetLastError();
            if (err == WSAETIMEDOUT) continue;          // expected
            if (err == WSAEINTR     ||
                err == WSAESHUTDOWN ||
                err == WSAENOTSOCK)   break;            // teardown
            framingErrors_.fetch_add(1, std::memory_order_relaxed);
            continue;
        }
        if (n != kMetisDgSize) {
            framingErrors_.fetch_add(1, std::memory_order_relaxed);
            continue;
        }
        const auto *u =
            reinterpret_cast<const std::uint8_t*>(buf.constData());

        // Metis header: magic + data + endpoint=0x06 (radio→host)
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
        // The HL2 gateware (ak4951v4 variant + every other HL2
        // gateware that shares this Verilog) implements the EP6
        // sequence counter as a 20-bit register, NOT 32-bit:
        //
        //   logic [19:0] ep6_seq_no = 20'h0;
        //   ...
        //   3'h3: udp_data_next = 8'h00;                       // hi byte
        //   3'h2: udp_data_next = {4'h00, ep6_seq_no[19:16]};  // hi 4b=0
        //   3'h1: udp_data_next = ep6_seq_no[15:8];
        //   3'h0: udp_data_next = ep6_seq_no[7:0];
        //         ep6_seq_no_next = ep6_seq_no + 'h1;          // wraps 2^20
        //
        // (gateware source verified 2026-05-20 — RTL of the HL2+
        // ak4951v4 variant running on the operator's bench.)
        //
        // At ~5053 dg/sec the counter wraps every 207.5 sec
        // (= 3 min 27 sec).  Treating the sequence as a 32-bit
        // monotonic field (per the generic HPSDR P1 spec) would
        // therefore flag every legitimate wrap as a "seq error",
        // burying real diagnostic value in deterministic noise —
        // operator HL2+ bench caught this on the first long run
        // (10 false alarms over 35 min = exactly 10 wraps).  We
        // mask to 20 bits when computing the next-expected so a
        // wrap from 0xFFFFF -> 0x00000 produces no false alarm
        // and every counted seqError is a REAL packet-loss event.
        // The mask is also forward-safe for the v0.4 ANAN family:
        // a hypothetical 32-bit-counter radio's wrap is one event
        // per 2^32 packets ≈ 5 days at typical rates, negligible.
        constexpr quint32 kSeqMask20 = 0x000FFFFF;

        if (firstPacket) {
            expectedSeq = (seq + 1) & kSeqMask20;
            firstPacket = false;
        } else {
            if (seq != expectedSeq) {
                seqErrors_.fetch_add(1, std::memory_order_relaxed);
            }
            expectedSeq = (seq + 1) & kSeqMask20;
        }
        // Both USB frames must begin 0x7F 0x7F 0x7F.
        if (u[ 8] != 0x7F || u[ 9] != 0x7F || u[ 10] != 0x7F ||
            u[520] != 0x7F || u[521] != 0x7F || u[522] != 0x7F) {
            framingErrors_.fetch_add(1, std::memory_order_relaxed);
            continue;
        }
        totalDg_.fetch_add(1, std::memory_order_relaxed);
        windowDg_.fetch_add(1, std::memory_order_relaxed);

        // ---- Step 2c/3d: parse DDC0 IQ -----------------------
        // Two USB frames per datagram at offsets 8 and 520; each
        // carries 19 sample slots starting at frame-offset 8 = 38
        // DDC0 IQ frames per datagram.  We (a) accumulate RMS for the
        // RX1 dBFS readout (Step 2c) and (b) pack interleaved
        // (I,Q,…) doubles in [-1,1) to hand the DSP engine once per
        // datagram (Step 3d).  Scaling (b0<<24|b1<<16|b2<<8) * 1/2^31
        // is byte-identical to the reference HL2 receive parse.
        constexpr int kFramesPerDatagram = 2 * 19;   // 38
        double iqScratch[2 * kFramesPerDatagram];
        int    iqIdx = 0;
        for (int usbFrame = 0; usbFrame < 2; ++usbFrame) {
            const std::uint8_t *frame = u + (usbFrame == 0 ? 8 : 520);
            for (int slot = 0; slot < 19; ++slot) {
                const std::uint8_t *sb = frame + 8 + slot * 26;
                // DDC0 I — bytes 0..2, packed into top 24 bits of int32
                const std::int32_t iRaw = static_cast<std::int32_t>(
                    (static_cast<std::uint32_t>(sb[0]) << 24) |
                    (static_cast<std::uint32_t>(sb[1]) << 16) |
                    (static_cast<std::uint32_t>(sb[2]) <<  8));
                // DDC0 Q — bytes 3..5
                const std::int32_t qRaw = static_cast<std::int32_t>(
                    (static_cast<std::uint32_t>(sb[3]) << 24) |
                    (static_cast<std::uint32_t>(sb[4]) << 16) |
                    (static_cast<std::uint32_t>(sb[5]) <<  8));
                const double iVal = static_cast<double>(iRaw) * kInv2Pow31;
                const double qVal = static_cast<double>(qRaw) * kInv2Pow31;
                iqScratch[iqIdx++] = iVal;
                iqScratch[iqIdx++] = qVal;
                rmsAcc += iVal * iVal + qVal * qVal;
                if (++rmsAccCount >= kRmsWindowSamples) {
                    const double meanSq =
                        rmsAcc / static_cast<double>(kRmsWindowSamples);
                    const double db = (meanSq > 0.0)
                        ? 10.0 * std::log10(meanSq)
                        : -200.0;
                    rx1DbFs_.store(db, std::memory_order_relaxed);
                    rmsAcc      = 0.0;
                    rmsAccCount = 0;
                }
            }
        }
        // Step 3d: hand this datagram's IQ to the DSP engine inline on
        // this RX worker thread (no Qt signal / no cross-thread queue).
        if (iqSink_) {
            iqSink_(iqScratch, kFramesPerDatagram);
        }
    }
}

// ----------------------------------------------------------------
// TX worker — dedicated OS thread, sends one START packet on
// entry then a 1032-byte EP2 keepalive every 2.6 ms (380 Hz)
// using a Win32 HIGH_RESOLUTION waitable timer with drift-
// corrected absolute scheduling.

void HL2Stream::txWorkerLoop(std::stop_token stop, SocketHandle sh,
                              QString ip) {
    const SOCKET s = static_cast<SOCKET>(sh);

    sockaddr_in dest{};
    dest.sin_family = AF_INET;
    dest.sin_port   = htons(kRadioPort);
    ::inet_pton(AF_INET, ip.toLatin1().constData(), &dest.sin_addr);

    // Send START.  Done from the TX thread (not from main or RX)
    // so every host→radio packet originates from one operation
    // context.  The radio responds with EP6 to whatever socket the
    // start came from — that's the shared socket the RX thread is
    // listening on, so EP6 routing is preserved.
    const QByteArray startPkt = buildControlPacket(true);
    if (::sendto(s, startPkt.constData(), startPkt.size(), 0,
                 reinterpret_cast<sockaddr*>(&dest),
                 sizeof(dest)) != startPkt.size()) {
        const int err = ::WSAGetLastError();
        QMetaObject::invokeMethod(this, [this, err]() {
            onFatalError(QStringLiteral("START: %1")
                         .arg(winsockError(err)));
        }, Qt::QueuedConnection);
        return;
    }
    QMetaObject::invokeMethod(this, [this]() {
        emit logLine(QStringLiteral(
            "  START sent (0xEFFE 0x04 0x01 + 60 zeros), "
            "EP2 keepalive engaging @380 Hz"));
    }, Qt::QueuedConnection);

    // High-resolution waitable timer (Win10 1803+ — operator is on
    // Windows 11 so supported).  Falls back to the legacy timer if
    // somehow unavailable; legacy granularity is ~1 ms which is
    // still plenty for the 13-sec gateware watchdog window.
    HANDLE timer = ::CreateWaitableTimerExW(
        nullptr, nullptr,
        CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
        TIMER_ALL_ACCESS);
    if (!timer) {
        timer = ::CreateWaitableTimerW(nullptr, FALSE, nullptr);
    }
    if (!timer) {
        QMetaObject::invokeMethod(this, [this]() {
            onFatalError(QStringLiteral(
                "EP2 timer create failed"));
        }, Qt::QueuedConnection);
        return;
    }

    LARGE_INTEGER qpcFreq;
    ::QueryPerformanceFrequency(&qpcFreq);
    const LONGLONG periodTicks = qpcFreq.QuadPart / kEp2RateHz;
    LARGE_INTEGER nextFire;
    ::QueryPerformanceCounter(&nextFire);
    nextFire.QuadPart += periodTicks;  // first fire 2.6 ms from now

    // Pre-allocated template — only seq bytes [4..7] change per send.
    QByteArray pkt = buildEp2KeepaliveTemplate();
    auto *pktBytes = reinterpret_cast<std::uint8_t*>(pkt.data());

    while (!stop.stop_requested()) {
        LARGE_INTEGER now;
        ::QueryPerformanceCounter(&now);
        const LONGLONG remainTicks = nextFire.QuadPart - now.QuadPart;

        if (remainTicks > 0) {
            // Convert QPC ticks → 100ns timer ticks.
            const LONGLONG remain100ns =
                remainTicks * 10000000LL / qpcFreq.QuadPart;
            // Cap the wait at 100 ms so stop_token has bounded
            // latency even if remainTicks somehow blows up.
            const LONGLONG wait100ns =
                std::min<LONGLONG>(remain100ns, 1000000LL);
            LARGE_INTEGER due;
            due.QuadPart = -wait100ns;
            ::SetWaitableTimer(timer, &due, 0,
                               nullptr, nullptr, FALSE);
            ::WaitForSingleObject(timer, INFINITE);
            // Re-check time — if we capped the wait, loop back
            // and wait the remaining slice.
            ::QueryPerformanceCounter(&now);
            if (now.QuadPart < nextFire.QuadPart) continue;
        }
        if (stop.stop_requested()) break;

        // Update the sequence number bytes in-place (BE u32).
        const quint32 seq =
            txSeq_.fetch_add(1, std::memory_order_relaxed);
        pktBytes[4] = static_cast<std::uint8_t>((seq >> 24) & 0xFF);
        pktBytes[5] = static_cast<std::uint8_t>((seq >> 16) & 0xFF);
        pktBytes[6] = static_cast<std::uint8_t>((seq >>  8) & 0xFF);
        pktBytes[7] = static_cast<std::uint8_t>( seq        & 0xFF);

        const int n = ::sendto(s, pkt.constData(), pkt.size(), 0,
                               reinterpret_cast<sockaddr*>(&dest),
                               sizeof(dest));
        if (n != pkt.size()) {
            txSendErrors_.fetch_add(1, std::memory_order_relaxed);
        } else {
            txTotalDg_.fetch_add(1, std::memory_order_relaxed);
            txWindowDg_.fetch_add(1, std::memory_order_relaxed);
        }

        // Schedule next fire — drift-corrected, NOT now+period.
        nextFire.QuadPart += periodTicks;

        // Resync if we've fallen way behind (e.g. system suspend,
        // ≥10 periods late).  Avoids a catch-up burst that would
        // saturate the gateware on resume from a long stall.
        ::QueryPerformanceCounter(&now);
        if (now.QuadPart - nextFire.QuadPart > periodTicks * 10) {
            nextFire.QuadPart = now.QuadPart + periodTicks;
        }
    }

    ::CloseHandle(timer);
}

} // namespace lyra::ipc
