// Lyra — Hermes Lite 2 / 2+ SDR transceiver (C++23 / Qt 6 rebuild).
//
// Step 1 entry point.  Opens a Qt Quick window backed by RHI
// (Vulkan/D3D12 on Windows; Metal on macOS; OpenGL fallback) and
// kicks off a C++ UDP discovery sweep on a dedicated worker
// QThread.  Discovery results stream into the window as they
// arrive.  No Python, no GIL, no in-process bottleneck on the
// wire path.

#include "hl2_discovery.h"
#include "hl2_stream.h"

#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickWindow>
#include <QSGRendererInterface>
#include <QtQml>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#endif

int main(int argc, char *argv[])
{
#ifdef _WIN32
    // Explicit WSAStartup before any native socket use (HL2Stream
    // uses raw WinSock2 — see src/hl2_stream.cpp).  Qt would do it
    // lazily for QUdpSocket, but we don't want to depend on Qt's
    // init ordering for our wire path.  WSAStartup is refcounted —
    // safe to call alongside Qt's.
    WSADATA wsa;
    ::WSAStartup(MAKEWORD(2, 2), &wsa);
#endif

    // Qt RHI backend selection.  Lyra targets Vulkan as the
    // primary graphics path per FEATURES.md §0 (cross-vendor /
    // cross-platform; Thetis is D3D12-only).  setGraphicsApi()
    // MUST be called BEFORE QGuiApplication construction.  If
    // Vulkan is unavailable at runtime (no driver loader / no
    // SDK / unsupported GPU) Qt RHI transparently falls back to
    // D3D12 → OpenGL — no operator-visible breakage.
    //
    // Setting QSG_INFO=1 makes Qt log the chosen backend on
    // startup so the operator can VERIFY which RHI is live
    // (look for "Using QRhi with backend Vulkan" in stdout).
    // qputenv is portable; std::setenv would be POSIX-only.
    qputenv("QSG_INFO", "1");
    QQuickWindow::setGraphicsApi(QSGRendererInterface::Vulkan);

    QGuiApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("Lyra"));
    app.setOrganizationName(QStringLiteral("N8SDR"));
    app.setOrganizationDomain(QStringLiteral("github.com/N8SDR1/Lyra-SDR"));

    // Discovery lives on the Qt main thread.  This is FINE: QUdpSocket
    // is fully async / event-loop-driven on Qt (no blocking recvfrom,
    // no GIL, no Python).  The "wire path on its OWN OS thread"
    // requirement applies to the real-time EP2 writer (380 Hz
    // cadence) and the RX loop (~5000 datagrams/s) — those land in
    // dedicated threads in later commits.  Discovery is one-shot
    // bursty UDP — it does not need its own thread, and putting it
    // on one breaks QML's main-thread Connections requirement
    // (QQmlEngine refuses cross-thread connect from QML).
    auto *discovery = new lyra::ipc::HL2Discovery(&app);

    // Step 2a: the stream object opens the EP6 RX path to a
    // selected radio on its OWN dedicated OS thread (std::jthread
    // inside HL2Stream — see hl2_stream.h for the locked
    // architecture rationale).  Exposed to QML as `Stream`.
    auto *stream = new lyra::ipc::HL2Stream(&app);

    // Expose the workers to QML as context properties so Main.qml
    // can bind to their signals + invoke their slots from buttons.
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(
        QStringLiteral("Discovery"), discovery);
    engine.rootContext()->setContextProperty(
        QStringLiteral("Stream"), stream);

    // Load the QML module's Main.qml entry.  URI 'Lyra' matches
    // qt_add_qml_module() in CMakeLists.txt.
    QObject::connect(
        &engine, &QQmlApplicationEngine::objectCreationFailed,
        &app, [](const QUrl &) { QCoreApplication::exit(-1); },
        Qt::QueuedConnection);
    engine.loadFromModule(QStringLiteral("Lyra"),
                          QStringLiteral("Main"));

    const int rc = app.exec();

#ifdef _WIN32
    ::WSACleanup();
#endif
    return rc;
}
