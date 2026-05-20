// Lyra — Hermes Lite 2 / 2+ SDR transceiver (C++23 / Qt 6 rebuild).
//
// Step 1 entry point.  Opens a Qt Quick window backed by RHI
// (Vulkan/D3D12 on Windows; Metal on macOS; OpenGL fallback) and
// kicks off a C++ UDP discovery sweep on a dedicated worker
// QThread.  Discovery results stream into the window as they
// arrive.  No Python, no GIL, no in-process bottleneck on the
// wire path.

#include "hl2_discovery.h"

#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QThread>
#include <QtQml>

int main(int argc, char *argv[])
{
    QGuiApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("Lyra"));
    app.setOrganizationName(QStringLiteral("N8SDR"));
    app.setOrganizationDomain(QStringLiteral("github.com/N8SDR1/Lyra-SDR"));

    // Discovery lives in its OWN worker thread — Qt main thread
    // (paint, QML scene graph, event loop) never blocks on UDP.
    QThread discoveryThread;
    discoveryThread.setObjectName(QStringLiteral("lyra-discovery"));
    auto *discovery = new lyra::ipc::HL2Discovery();
    discovery->moveToThread(&discoveryThread);
    QObject::connect(&discoveryThread, &QThread::finished,
                     discovery, &QObject::deleteLater);
    discoveryThread.start();

    // Expose the worker to QML as a context property so Main.qml
    // can bind to its signals + invoke scan() from a Button click.
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(
        QStringLiteral("Discovery"), discovery);

    // Load the QML module's Main.qml entry.  URI 'Lyra' matches
    // qt_add_qml_module() in CMakeLists.txt.
    QObject::connect(
        &engine, &QQmlApplicationEngine::objectCreationFailed,
        &app, [](const QUrl &) { QCoreApplication::exit(-1); },
        Qt::QueuedConnection);
    engine.loadFromModule(QStringLiteral("Lyra"),
                          QStringLiteral("Main"));

    const int rc = app.exec();

    // Clean shutdown — stop the worker thread before destruction.
    discoveryThread.quit();
    discoveryThread.wait(2000);
    return rc;
}
