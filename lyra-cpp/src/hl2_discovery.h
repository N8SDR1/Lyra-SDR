// Lyra — HPSDR Protocol 1 discovery (Hermes Lite 2 / 2+).
//
// Mirrors the verified byte layout from the existing Python
// implementation at ../lyra/protocol/discovery.py (which has been
// running cleanly on N8SDR's real HL2+ for months).  Translated to
// native C++ + QtNetwork.  Runs on its OWN worker QThread — the
// wire path NEVER touches the Qt main thread.
//
// Protocol summary:
//   Send: 63-byte UDP packet
//     bytes [0..2] = 0xEF 0xFE 0x02
//     bytes [3..62] = zero padding
//     destination = 255.255.255.255:1024 (broadcast)
//
//   Reply (>= 24 bytes):
//     bytes [0..1] = 0xEF 0xFE (magic — reject anything else)
//     byte  [2]    = status (0x02 = idle, 0x03 = busy, anything
//                            else = not an HPSDR reply)
//     bytes [3..8] = MAC (6 bytes)
//     byte  [9]    = code_version  (gateware major)
//     byte  [10]   = board_id      (6 = HermesLite family — both
//                                   HL2 and HL2+ report the same ID)
//   HL2-specific extras (MI0BOT-fork bytes, present on N8SDR's unit):
//     byte  [11]   = ee_config
//     byte  [12]   = ee_config_reserved
//     bytes [13..16] = fixed_ip (stored REVERSED on the wire)
//     byte  [19]   = num_rxs
//     byte  [21]   = beta_version
//
// Multi-NIC: walks every local IPv4 interface, binds a socket per
// interface, broadcasts from each.  Without this, a host with both
// Ethernet + Wi-Fi silently misses the HL2 on the wrong NIC because
// QUdpSocket on 0.0.0.0 lets the OS pick exactly one route.

#pragma once

#include <QObject>
#include <QString>
#include <QHostAddress>
#include <QUdpSocket>
#include <QTimer>
#include <QList>
#include <QSet>
#include <QVariantMap>
#include <cstdint>
#include <memory>
#include <vector>

namespace lyra::ipc {

struct RadioInfo {
    QString ip;
    QString mac;
    int     boardId      = 0;
    QString boardName;
    int     codeVersion  = 0;
    int     betaVersion  = 0;
    int     numRxs       = 0;
    bool    isBusy       = false;
    int     eeConfig     = 0;
    QString fixedIpHl2;
};

class HL2Discovery : public QObject {
    Q_OBJECT
    // Exposed to QML via QQmlContext::setContextProperty("Discovery",
    // ...) in main.cpp (simpler than QML_ELEMENT registration for
    // Step 1 — and lets the worker live on its own QThread).

public:
    explicit HL2Discovery(QObject *parent = nullptr);
    ~HL2Discovery() override;

    // Radio memory: persist / restore the last opened radio's full
    // record so launch can show it in the Found-radios list.  Done in
    // C++ QSettings (not QML's QtCore Settings, whose plugin isn't in
    // the local deploy) — same store the auto-connect radio/lastIp
    // uses.  savedRadio() returns a QVariantMap with the same keys the
    // radioModel uses (ip/mac/boardName/codeVersion/betaVersion/busy/
    // numRxs); the "ip" key is empty when nothing is remembered.
    Q_INVOKABLE void rememberRadio(const QString &ip, const QString &mac,
                                   const QString &boardName,
                                   int codeVersion, int betaVersion,
                                   bool busy, int numRxs);
    Q_INVOKABLE QVariantMap savedRadio() const;

public slots:
    // Fire one discovery sweep.  Safe to call repeatedly; each
    // sweep opens its own sockets, broadcasts, listens, closes.
    void scan(double timeoutSeconds = 1.5, int attempts = 2);

signals:
    // Emitted once per UNIQUE radio found (de-duped by MAC).
    void radioFound(QString ip, QString mac, QString boardName,
                    int codeVersion, int betaVersion,
                    bool busy, int numRxs);
    // Emitted when the sweep finishes (timeout reached, all
    // attempts done).  `count` = unique radios found.
    void scanFinished(int count);
    // Diagnostic log line — fed to the QML view for the operator.
    void logLine(QString line);

private slots:
    void onReadyRead();
    void onSweepDeadline();

private:
    QList<QHostAddress> localIPv4Interfaces() const;
    static QByteArray buildDiscoveryPacket();
    bool parseReply(const QByteArray &data,
                    const QHostAddress &sender,
                    RadioInfo &out) const;
    void sendBroadcastFromAllSockets();

    // std::vector (not QList) because QList is value-semantic
    // (implicitly shared / copy-on-write) and unique_ptr is
    // move-only — QList tries to copy elements internally.
    std::vector<std::unique_ptr<QUdpSocket>> sockets_;
    QSet<QString>                       foundMacs_;
    QTimer                              deadline_;
    QTimer                              attemptTimer_;
    int                                 attemptsRemaining_ = 0;
    int                                 totalFound_       = 0;

    static constexpr quint16 kDiscoveryPort = 1024;
    static constexpr int     kPacketLen     = 63;
    static constexpr int     kBoardHL2      = 6;
};

} // namespace lyra::ipc
