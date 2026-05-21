// Lyra — HPSDR Protocol 1 discovery implementation.  See
// hl2_discovery.h for the protocol summary + the operator-bench-
// verified Python reference at ../lyra/protocol/discovery.py.

#include "hl2_discovery.h"

#include <QNetworkInterface>
#include <QSettings>
#include <QtEndian>
#include <QtDebug>
#include <algorithm>

namespace lyra::ipc {

namespace {
QString boardName(int boardId) {
    switch (boardId) {
        case 0:  return QStringLiteral("Atlas");
        case 1:  return QStringLiteral("Hermes");
        case 2:  return QStringLiteral("HermesII");
        case 4:  return QStringLiteral("Angelia");
        case 5:  return QStringLiteral("Orion");
        case 6:  return QStringLiteral("HermesLite");
        case 10: return QStringLiteral("OrionMKII");
        default: return QStringLiteral("Unknown(%1)").arg(boardId);
    }
}
} // namespace

void HL2Discovery::rememberRadio(const QString &ip, const QString &mac,
                                 const QString &boardName_,
                                 int codeVersion, int betaVersion,
                                 bool busy, int numRxs) {
    QSettings s;
    s.beginGroup(QStringLiteral("lastRadio"));
    s.setValue(QStringLiteral("ip"), ip);
    s.setValue(QStringLiteral("mac"), mac);
    s.setValue(QStringLiteral("boardName"), boardName_);
    s.setValue(QStringLiteral("codeVersion"), codeVersion);
    s.setValue(QStringLiteral("betaVersion"), betaVersion);
    s.setValue(QStringLiteral("busy"), busy);
    s.setValue(QStringLiteral("numRxs"), numRxs);
    s.endGroup();
}

QVariantMap HL2Discovery::savedRadio() const {
    QSettings s;
    s.beginGroup(QStringLiteral("lastRadio"));
    QVariantMap m;
    // Keys match the QML radioModel fields so QML can append directly.
    m[QStringLiteral("ip")]          = s.value(QStringLiteral("ip"));
    m[QStringLiteral("mac")]         = s.value(QStringLiteral("mac"));
    m[QStringLiteral("boardName")]   = s.value(QStringLiteral("boardName"));
    m[QStringLiteral("codeVersion")] = s.value(QStringLiteral("codeVersion"), 0);
    m[QStringLiteral("betaVersion")] = s.value(QStringLiteral("betaVersion"), 0);
    // busy is a LIVE discovery state (another client holds the radio).
    // The remembered entry can't know it without a fresh sweep, so
    // never show stale BUSY — Lyra's own connection is conveyed by the
    // green "connected" border + Close button instead.
    m[QStringLiteral("busy")]        = false;
    m[QStringLiteral("numRxs")]      = s.value(QStringLiteral("numRxs"), 0);
    s.endGroup();
    return m;
}

HL2Discovery::HL2Discovery(QObject *parent) : QObject(parent) {
    deadline_.setSingleShot(true);
    connect(&deadline_, &QTimer::timeout,
            this, &HL2Discovery::onSweepDeadline);

    attemptTimer_.setSingleShot(false);
    connect(&attemptTimer_, &QTimer::timeout, this, [this]() {
        if (attemptsRemaining_-- > 0) {
            sendBroadcastFromAllSockets();
        } else {
            attemptTimer_.stop();
        }
    });
}

HL2Discovery::~HL2Discovery() = default;

QByteArray HL2Discovery::buildDiscoveryPacket() {
    QByteArray pkt(kPacketLen, char{0});
    pkt[0] = static_cast<char>(0xEF);
    pkt[1] = static_cast<char>(0xFE);
    pkt[2] = static_cast<char>(0x02);
    return pkt;
}

QList<QHostAddress> HL2Discovery::localIPv4Interfaces() const {
    QList<QHostAddress> out;
    for (const QNetworkInterface &iface : QNetworkInterface::allInterfaces()) {
        if (!(iface.flags() & QNetworkInterface::IsUp) ||
            !(iface.flags() & QNetworkInterface::IsRunning)) {
            continue;
        }
        if (iface.flags() & QNetworkInterface::IsLoopBack) continue;
        for (const QNetworkAddressEntry &entry : iface.addressEntries()) {
            const QHostAddress addr = entry.ip();
            if (addr.protocol() != QAbstractSocket::IPv4Protocol) continue;
            const QString s = addr.toString();
            if (s.startsWith(QStringLiteral("127."))) continue;
            if (s.startsWith(QStringLiteral("169.254."))) continue;
            out.append(addr);
        }
    }
    return out;
}

bool HL2Discovery::parseReply(const QByteArray &data,
                              const QHostAddress &sender,
                              RadioInfo &out) const {
    if (data.size() < 24) return false;
    const auto u = reinterpret_cast<const std::uint8_t*>(data.constData());
    if (u[0] != 0xEF || u[1] != 0xFE) return false;
    const std::uint8_t status = u[2];
    if (status != 0x02 && status != 0x03) return false;

    out.ip = sender.toString();
    // QHostAddress::toString() can prefix IPv4 with "::ffff:" when
    // arriving on a dual-stack socket — strip that.
    if (out.ip.startsWith(QStringLiteral("::ffff:"))) {
        out.ip = out.ip.mid(7);
    }
    out.mac = QString::asprintf(
        "%02X:%02X:%02X:%02X:%02X:%02X",
        u[3], u[4], u[5], u[6], u[7], u[8]);
    out.codeVersion = u[9];
    out.boardId     = u[10];
    out.boardName   = boardName(out.boardId);
    out.isBusy      = (status == 0x03);

    if (out.boardId == kBoardHL2) {
        out.eeConfig = u[11];
        if (data.size() > 16) {
            out.fixedIpHl2 = QString::asprintf(
                "%d.%d.%d.%d", u[16], u[15], u[14], u[13]);
        }
        if (data.size() > 19) out.numRxs      = u[19];
        if (data.size() > 21) out.betaVersion = u[21];
    } else {
        if (data.size() > 20) out.numRxs = u[20];
    }
    return true;
}

void HL2Discovery::scan(double timeoutSeconds, int attempts) {
    foundMacs_.clear();
    totalFound_ = 0;
    sockets_.clear();
    attemptsRemaining_ = std::max(0, attempts - 1);

    const QList<QHostAddress> ifaces = localIPv4Interfaces();
    if (ifaces.isEmpty()) {
        emit logLine(QStringLiteral("ERROR: no usable IPv4 interfaces"));
        emit scanFinished(0);
        return;
    }
    emit logLine(QStringLiteral("Scanning %1 local interface(s)...")
                 .arg(ifaces.size()));

    for (const QHostAddress &addr : ifaces) {
        auto sock = std::make_unique<QUdpSocket>(this);
        // SO_REUSEADDR so multiple interface-bound sockets coexist
        // on the same port (the HL2 reply lands on whichever socket
        // its routing table associated with the broadcast).
        if (!sock->bind(addr, 0,
                        QAbstractSocket::ShareAddress |
                        QAbstractSocket::ReuseAddressHint)) {
            emit logLine(QStringLiteral("  bind FAILED on %1: %2")
                         .arg(addr.toString(), sock->errorString()));
            continue;
        }
        connect(sock.get(), &QUdpSocket::readyRead,
                this, &HL2Discovery::onReadyRead);
        emit logLine(QStringLiteral("  socket bound to %1:%2")
                     .arg(addr.toString())
                     .arg(sock->localPort()));
        sockets_.push_back(std::move(sock));
    }

    if (sockets_.empty()) {
        emit logLine(QStringLiteral("ERROR: zero sockets bound; aborting"));
        emit scanFinished(0);
        return;
    }

    // First broadcast immediately, then schedule retries at half the
    // total timeout window (so a 2-attempt 1.5s scan retries at 0.75s).
    sendBroadcastFromAllSockets();
    if (attemptsRemaining_ > 0) {
        const int periodMs = std::max(100,
            static_cast<int>(timeoutSeconds * 1000.0 / attempts));
        attemptTimer_.start(periodMs);
    }

    deadline_.start(static_cast<int>(timeoutSeconds * 1000.0));
}

void HL2Discovery::sendBroadcastFromAllSockets() {
    const QByteArray pkt = buildDiscoveryPacket();
    for (const auto &sock : sockets_) {
        const qint64 sent = sock->writeDatagram(
            pkt, QHostAddress::Broadcast, kDiscoveryPort);
        if (sent != pkt.size()) {
            emit logLine(QStringLiteral("  send via %1 short/failed: %2")
                         .arg(sock->localAddress().toString(),
                              sock->errorString()));
        }
    }
}

void HL2Discovery::onReadyRead() {
    auto *sock = qobject_cast<QUdpSocket*>(sender());
    if (!sock) return;
    while (sock->hasPendingDatagrams()) {
        QByteArray buf;
        buf.resize(static_cast<int>(sock->pendingDatagramSize()));
        QHostAddress sender;
        quint16 port = 0;
        const qint64 n = sock->readDatagram(buf.data(), buf.size(),
                                            &sender, &port);
        if (n < 0) continue;
        buf.resize(static_cast<int>(n));

        RadioInfo info;
        if (!parseReply(buf, sender, info)) {
            emit logLine(QStringLiteral(
                "  reply from %1: %2 bytes (not HPSDR — ignored)")
                .arg(sender.toString()).arg(buf.size()));
            continue;
        }
        if (foundMacs_.contains(info.mac)) continue;
        foundMacs_.insert(info.mac);
        ++totalFound_;
        emit logLine(QStringLiteral(
            "  FOUND: %1  %2  %3  gw=v%4.%5  busy=%6  rxs=%7")
            .arg(info.ip, info.mac, info.boardName)
            .arg(info.codeVersion).arg(info.betaVersion)
            .arg(info.isBusy ? QStringLiteral("yes")
                              : QStringLiteral("no"))
            .arg(info.numRxs));
        emit radioFound(info.ip, info.mac, info.boardName,
                        info.codeVersion, info.betaVersion,
                        info.isBusy, info.numRxs);
    }
}

void HL2Discovery::onSweepDeadline() {
    attemptTimer_.stop();
    sockets_.clear();
    emit logLine(QStringLiteral("Discovery complete: %1 radio(s) found")
                 .arg(totalFound_));
    emit scanFinished(totalFound_);
}

} // namespace lyra::ipc
