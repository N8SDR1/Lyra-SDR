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
#include "wdsp_engine.h"
#include "wdsp_native.h"

#include <QCoreApplication>
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickWindow>
#include <QSGRendererInterface>
#include <QTimer>
#include <QtQml>

#include <string_view>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#endif

int main(int argc, char *argv[])
{
    // ---- Subprocess entry point (Step 3c-i) ----
    // When invoked as `lyra.exe --build-wisdom <dir>` we are a
    // SHORT-LIVED CHILD spawned by the main Lyra process to build
    // the FFTW wisdom file out-of-process.  AllocConsole inside
    // WDSPwisdom would corrupt the parent's stdout if we ran in
    // the parent process; this subprocess isolates the damage.
    //
    // Must be FIRST in main() — before QGuiApplication, before
    // any Qt RHI or networking init.  We're a console-only mode
    // here: no window, no QML, no event loop.  Just LoadLibrary,
    // call WDSPwisdom, exit.
    if (argc == 3 && std::string_view(argv[1]) == "--build-wisdom") {
        // QCoreApplication needed for QStandardPaths + QString
        // I/O.  No event loop, no GUI.
        QCoreApplication app(argc, argv);
        app.setApplicationName(QStringLiteral("Lyra-cpp"));
        app.setOrganizationName(QStringLiteral("N8SDR"));
        lyra::dsp::WdspNative builder;
        return builder.runWisdomBuilderEntryPoint(
            QString::fromLocal8Bit(argv[2]));
    }

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
    // applicationName is the LEAF segment in QStandardPaths paths
    // (%APPDATA%\<organizationName>\<applicationName>\…).  We use
    // "Lyra-cpp" so we share NO directories with Python Lyra
    // (which uses "Lyra") — wisdom isolation per CLAUDE.md §15.26.
    app.setApplicationName(QStringLiteral("Lyra-cpp"));
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

    // Step 3a: bundle the WDSP DSP engine.  Created here so the
    // QML context-property is available when Main.qml loads, but
    // load() itself is DEFERRED until AFTER engine.loadFromModule
    // — otherwise the logLine signal we emit on success/failure
    // arrives before the QML Connections are wired and the
    // operator never sees the result.  Step 3a's scope is
    // load-or-fail only — no symbols resolved yet (Step 3b), no
    // DSP yet (Step 3c+).
    auto *wdsp = new lyra::dsp::WdspNative(&app);

    // Step 3c-ii: the WDSP RX channel engine.  Owns the lifecycle of
    // a single WDSP receiver channel (OpenChannel + config + start /
    // stop + close).  Created here so the QML context-property exists
    // when Main.qml loads; openRx1() is DEFERRED until after the DLL
    // loads + wisdom is ensured (below) so the channel-open is fast.
    auto *wdspEngine = new lyra::dsp::WdspEngine(wdsp, &app);

    // Step 3d: route DDC0 baseband IQ from the stream's RX worker
    // thread into the DSP engine.  Set BEFORE any open() so the worker
    // sees the sink the moment it starts.  feedIq() runs synchronously
    // on the RX worker thread (no Qt cross-thread queueing) and guards
    // internally on the channel being open.
    stream->setIqSink([wdspEngine](const double *iq, int n) {
        wdspEngine->feedIq(iq, n);
    });

    // Stop the RX worker on quit BEFORE the QObject children are
    // destroyed.  The IQ sink (above) calls into wdspEngine from the
    // worker thread; aboutToQuit fires while every object is still
    // alive, so closing the stream here joins the worker and
    // guarantees no feedIq() runs into a half-torn-down engine /
    // freed audio ring (see WdspEngine::stopAudio).
    QObject::connect(&app, &QGuiApplication::aboutToQuit,
                     stream, &lyra::ipc::HL2Stream::close);

    // Expose the workers to QML as context properties so Main.qml
    // can bind to their signals + invoke their slots from buttons.
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(
        QStringLiteral("Discovery"), discovery);
    engine.rootContext()->setContextProperty(
        QStringLiteral("Stream"), stream);
    engine.rootContext()->setContextProperty(
        QStringLiteral("Wdsp"), wdsp);
    engine.rootContext()->setContextProperty(
        QStringLiteral("WdspEngine"), wdspEngine);

    // Load the QML module's Main.qml entry.  URI 'Lyra' matches
    // qt_add_qml_module() in CMakeLists.txt.
    QObject::connect(
        &engine, &QQmlApplicationEngine::objectCreationFailed,
        &app, [](const QUrl &) { QCoreApplication::exit(-1); },
        Qt::QueuedConnection);
    engine.loadFromModule(QStringLiteral("Lyra"),
                          QStringLiteral("Main"));

    // Defer the WDSP load / wisdom / channel-open to the FIRST
    // event-loop iteration via a zero-delay single-shot.  These steps
    // emit status via logLine signals; if we run them here (before
    // app.exec()) the QML scene isn't live yet, so the lines reach the
    // console via qInfo() but NOT the in-UI Log panel (operator-
    // observed — only [disc]/[strm], which fire after exec, showed).
    // Posting to the event loop means every [wdsp] line lands in the
    // Log panel exactly like [disc]/[strm].
    QTimer::singleShot(0, &app, [wdsp, wdspEngine]() {
        if (wdsp->load()) {
            // Step 3c-i: ensure FFTW wisdom is loaded BEFORE the first
            // OpenChannel anywhere.  Without it, WDSP's PATIENT
            // planning runs in-process on first channel-open and
            // freezes the UI for several minutes.  ensureWisdom()
            // either imports a cached file (fast) or spawns a
            // subprocess to build one (slow but the work is in the
            // child process, so the parent doesn't run FFTW_PATIENT
            // in-process).
            wdsp->ensureWisdom();

            // Step 3c-ii: with the DLL loaded + wisdom in place, open
            // RX1 as a live WDSP channel.  Channel-lifecycle proof
            // only — no IQ flows through it yet (Step 3d wires the RX
            // worker -> fexchange0).  The engine's destructor closes
            // the channel at app exit.
            wdspEngine->openRx1();
        }
    });

    const int rc = app.exec();

#ifdef _WIN32
    ::WSACleanup();
#endif
    return rc;
}
