// Lyra — WDSP DLL loader implementation (Step 3a).  See wdsp_native.h
// for the locked architecture + scope.

#include "wdsp_native.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <QCoreApplication>
#include <QDebug>
#include <QDir>
#include <QElapsedTimer>
#include <QFileInfo>
#include <QProcess>
#include <QStandardPaths>
#include <QString>
#include <QStringList>

namespace lyra::dsp {

namespace {

// Format a Windows error code into a human-readable message via
// FormatMessageW — mirrors the helper in hl2_stream.cpp.
QString winError(DWORD code) {
    wchar_t *buf = nullptr;
    const DWORD len = ::FormatMessageW(
        FORMAT_MESSAGE_ALLOCATE_BUFFER |
        FORMAT_MESSAGE_FROM_SYSTEM     |
        FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, code,
        MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
        reinterpret_cast<wchar_t*>(&buf), 0, nullptr);
    QString descr;
    if (len && buf) {
        descr = QString::fromWCharArray(buf, len).trimmed();
        ::LocalFree(buf);
    }
    return descr.isEmpty()
        ? QStringLiteral("Win32 error %1").arg(code)
        : QStringLiteral("Win32 error %1: %2").arg(code).arg(descr);
}

} // namespace

WdspNative::WdspNative(QObject *parent) : QObject(parent) {}

WdspNative::~WdspNative() {
    unload();
}

bool WdspNative::load() {
    if (handle_ != nullptr) {
        return true;  // already loaded — idempotent
    }

    // Resolve `<exe-dir>/_native/`.  applicationDirPath() returns
    // the canonical native path on Windows so this is safe with
    // spaces / non-ASCII operator usernames.
    const QString exeDir   = QCoreApplication::applicationDirPath();
    const QString nativeDir = QDir::cleanPath(exeDir +
                              QStringLiteral("/_native"));
    const QString dllPath   = QDir::cleanPath(nativeDir +
                              QStringLiteral("/wdsp.dll"));

    if (!QFileInfo::exists(dllPath)) {
        loadError_ = QStringLiteral("wdsp.dll not found at %1")
                     .arg(dllPath);
        emitLog(QStringLiteral("[wdsp] LOAD FAILED: %1")
                .arg(loadError_));
        emit loadedChanged();
        return false;
    }

    // Add _native/ to the dynamic-link search path so wdsp.dll's
    // dependent DLLs (libfftw3-3.dll etc.) resolve from the same
    // directory.  The Python tree uses `os.add_dll_directory`;
    // C++ equivalent is `AddDllDirectory` (Win10+/Win8+) — it
    // APPENDS to the search path rather than replacing it (vs the
    // older `SetDllDirectory` which clobbers + has subtle side
    // effects).  Need LOAD_LIBRARY_SEARCH_USER_DIRS in the
    // LoadLibraryExW call for AddDllDirectory's entries to take
    // effect.  Operator is on Windows 11 so this path is supported.
    const std::wstring nativeDirW = nativeDir.toStdWString();
    DLL_DIRECTORY_COOKIE cookie =
        ::AddDllDirectory(nativeDirW.c_str());
    if (!cookie) {
        // Non-fatal — LoadLibraryExW may still find it via the
        // default search.  Log the issue and continue.
        const DWORD err = ::GetLastError();
        emit logLine(QStringLiteral(
            "[wdsp] AddDllDirectory failed (continuing): %1")
            .arg(winError(err)));
    }

    const std::wstring dllPathW = dllPath.toStdWString();
    HMODULE h = ::LoadLibraryExW(
        dllPathW.c_str(),
        nullptr,
        LOAD_LIBRARY_SEARCH_DEFAULT_DIRS |
        LOAD_LIBRARY_SEARCH_USER_DIRS    |
        LOAD_LIBRARY_SEARCH_APPLICATION_DIR);

    if (cookie) {
        // We don't need the cookie to persist past LoadLibraryExW
        // — wdsp.dll's deps are resolved at this point.  Remove
        // to keep the search-path list clean.
        ::RemoveDllDirectory(cookie);
    }

    if (!h) {
        const DWORD err = ::GetLastError();
        loadError_ = winError(err);
        emitLog(QStringLiteral(
            "[wdsp] LOAD FAILED: LoadLibraryExW(%1): %2")
            .arg(dllPath, loadError_));
        emit loadedChanged();
        return false;
    }

    handle_     = static_cast<void*>(h);
    loadedFrom_ = dllPath;
    loadError_.clear();
    emitLog(QStringLiteral("[wdsp] LOADED: %1").arg(dllPath));

    // Step 3b: resolve the minimum WDSP entry points via
    // GetProcAddress.  On any miss we unload + return false so the
    // operator sees an explicit symbol-resolution failure rather
    // than a deferred crash at first use in Step 3c.
    if (!resolveSymbols()) {
        ::FreeLibrary(h);
        handle_ = nullptr;
        loadedFrom_.clear();
        emit loadedChanged();
        return false;
    }

    emit loadedChanged();
    return true;
}

void WdspNative::emitLog(const QString &line) {
    // Mirror every log line to both the QML log panel (via the
    // logLine signal) AND the host's stdout via qInfo() -- the
    // operator runs lyra.exe from a console specifically to
    // capture diagnostics, so a one-stop place to read every
    // status line is operator-friendly.  Cheap (one printf-like
    // call); no production concern.
    qInfo("%s", qPrintable(line));
    emit logLine(line);
}

bool WdspNative::resolveSymbols() {
    HMODULE mod = static_cast<HMODULE>(handle_);
    if (!mod) return false;

    QStringList missing;
    int found = 0;
    int total = 0;

    // Wrapper resolves one symbol into the strongly-typed function
    // pointer, increments the running tallies, and records the
    // name on miss.  reinterpret_cast through FARPROC is the
    // standard Win32 idiom (the MS docs explicitly bless this for
    // GetProcAddress assignment to function-pointer types).
    auto resolve = [&](auto &fnPtr, const char *name) {
        ++total;
        using FnT = std::remove_reference_t<decltype(fnPtr)>;
        FARPROC p = ::GetProcAddress(mod, name);
        if (p) {
            fnPtr = reinterpret_cast<FnT>(p);
            ++found;
        } else {
            fnPtr = nullptr;
            missing << QString::fromLatin1(name);
        }
    };

    resolve(api_.OpenChannel,         "OpenChannel");
    resolve(api_.CloseChannel,        "CloseChannel");
    resolve(api_.SetChannelState,     "SetChannelState");
    resolve(api_.fexchange0,          "fexchange0");
    resolve(api_.SetRXAMode,          "SetRXAMode");
    resolve(api_.RXASetPassband,      "RXASetPassband");
    resolve(api_.SetRXAAGCMode,       "SetRXAAGCMode");
    resolve(api_.SetRXAPanelBinaural, "SetRXAPanelBinaural");
    resolve(api_.WDSPwisdom,          "WDSPwisdom");
    resolve(api_.SetRXAAGCThresh,     "SetRXAAGCThresh");
    resolve(api_.SetRXAAGCSlope,      "SetRXAAGCSlope");
    resolve(api_.SetRXAPanelGain1,    "SetRXAPanelGain1");
    resolve(api_.XCreateAnalyzer,        "XCreateAnalyzer");
    resolve(api_.DestroyAnalyzer,        "DestroyAnalyzer");
    resolve(api_.SetAnalyzer,            "SetAnalyzer");
    resolve(api_.Spectrum0,              "Spectrum0");
    resolve(api_.GetPixels,              "GetPixels");
    resolve(api_.SetDisplayDetectorMode, "SetDisplayDetectorMode");
    resolve(api_.SetDisplayAverageMode,  "SetDisplayAverageMode");
    resolve(api_.SetDisplayNumAverage,   "SetDisplayNumAverage");
    resolve(api_.SetDisplayAvBackmult,   "SetDisplayAvBackmult");

    if (!missing.isEmpty()) {
        loadError_ = QStringLiteral(
            "symbols resolved %1/%2 -- MISSING: %3")
            .arg(found).arg(total).arg(missing.join(QStringLiteral(", ")));
        emitLog(QStringLiteral("[wdsp] %1").arg(loadError_));
        return false;
    }

    emitLog(QStringLiteral("[wdsp] symbols: %1/%2 resolved")
            .arg(found).arg(total));
    return true;
}

void WdspNative::unload() {
    if (handle_ == nullptr) return;
    ::FreeLibrary(static_cast<HMODULE>(handle_));
    handle_ = nullptr;
    loadedFrom_.clear();
    loadError_.clear();
    emit loadedChanged();
}

// ---------------------------------------------------------------
// Step 3c-i: FFTW WISDOM plumbing.
// ---------------------------------------------------------------

QString WdspNative::wisdomDir() {
    // Lyra-C++-PRIVATE directory.  Qt's GenericDataLocation
    // resolves to %APPDATA%\ on Windows; we then carve out our
    // own "N8SDR/Lyra-cpp/fftw/" subdir so we share with NEITHER
    // Python Lyra (which uses .../N8SDR/Lyra/fftw/) NOR any other
    // HPSDR app.  Isolation-by-directory per CLAUDE.md §15.26
    // (the wisdom-file format isn't versioned; different WDSP
    // builds can produce mutually-incompatible cached plans, so
    // cross-app sharing is a hidden footgun we explicitly avoid).
    const QString base = QStandardPaths::writableLocation(
        QStandardPaths::GenericDataLocation);
    return QDir::cleanPath(base +
        QStringLiteral("/N8SDR/Lyra-cpp/fftw"));
}

namespace {

// Hard-coded filename per WDSP source (wisdom.c) — same name
// every HPSDR app produces; isolation is purely by directory.
constexpr const char *kWisdomFilename = "wdspWisdom00";

bool wisdomFileExists(const QString &dir) {
    return QFileInfo::exists(QDir(dir).filePath(
        QString::fromLatin1(kWisdomFilename)));
}

}  // namespace

int WdspNative::runWisdomBuilderEntryPoint(const QString &targetDir) {
    // Subprocess entry point.  Parent process spawned us with:
    //
    //   lyra.exe --build-wisdom <targetDir>
    //
    // We need wdsp.dll loaded + the WDSPwisdom symbol resolved
    // before we can call it.  Reuse our normal load() path — it
    // walks the same _native/ folder + resolves all 9 symbols.
    // On success we call WDSPwisdom(<targetDir>) which does the
    // multi-minute FFTW_PATIENT search inside WDSP, writes
    // wdspWisdom00 in the target dir, then returns.
    //
    // AllocConsole + FreeConsole inside WDSPwisdom won't bite us
    // here — we redirect stdio to nullDevice() in the parent's
    // QProcess setup, and we don't read any output from this
    // process.  Stdout corruption is irrelevant.
    if (!load()) {
        return 1;   // DLL couldn't be loaded
    }
    if (!api_.WDSPwisdom) {
        return 1;   // unexpected — load() already validated this
    }
    QDir().mkpath(targetDir);
    // WDSP appends "wdspWisdom00" to the dir string with strcat,
    // so the dir argument MUST end in a path separator.  Use
    // native separators + ANSI codepage for filesystem APIs.
    QString dirArg = targetDir;
    if (!dirArg.endsWith(QLatin1Char('/')) &&
        !dirArg.endsWith(QLatin1Char('\\'))) {
        dirArg += QLatin1Char('/');
    }
    QByteArray dirBytes =
        QDir::toNativeSeparators(dirArg).toLocal8Bit();
    api_.WDSPwisdom(dirBytes.data());
    return 0;
}

bool WdspNative::ensureWisdom() {
    if (!isLoaded()) {
        emitLog(QStringLiteral(
            "[wdsp] wisdom: cannot ensure — DLL not loaded"));
        return false;
    }
    if (!api_.WDSPwisdom) {
        emitLog(QStringLiteral(
            "[wdsp] wisdom: cannot ensure — WDSPwisdom symbol "
            "not resolved"));
        return false;
    }

    const QString dir = wisdomDir();
    QDir().mkpath(dir);

    // ---- Fast path: cache exists, import in-process. ----
    // We're a console build right now (CMakeLists.txt keeps
    // WIN32_EXECUTABLE OFF for the diagnostic build), and an
    // import-only WDSPwisdom call is sub-100ms anyway, so any
    // AllocConsole stdout hijack is brief + harmless.  When we
    // flip to a --windowed binary, we'll route this through the
    // subprocess too.  For now keep it in-process for simplicity
    // + bench-visibility.
    if (wisdomFileExists(dir)) {
        emitLog(QStringLiteral(
            "[wdsp] wisdom: loading cached plans from %1").arg(dir));
        QElapsedTimer t; t.start();
        QString dirArg = dir;
        if (!dirArg.endsWith(QLatin1Char('/'))) {
            dirArg += QLatin1Char('/');
        }
        QByteArray dirBytes = QDir::toNativeSeparators(dirArg)
                              .toLocal8Bit();
        api_.WDSPwisdom(dirBytes.data());
        emitLog(QStringLiteral(
            "[wdsp] wisdom: loaded in %1 ms").arg(t.elapsed()));
        return true;
    }

    // ---- Slow path: no cache, spawn the subprocess builder. ----
    emitLog(QStringLiteral(
        "[wdsp] wisdom: building (one-time, may take several "
        "minutes — the window stays unresponsive while planning "
        "runs; this is normal)…"));
    emitLog(QStringLiteral(
        "[wdsp] wisdom: target dir = %1").arg(dir));

    const QString exe = QCoreApplication::applicationFilePath();
    QProcess builder;
    builder.setStandardOutputFile(QProcess::nullDevice());
    builder.setStandardErrorFile(QProcess::nullDevice());
    QStringList args;
    args << QStringLiteral("--build-wisdom") << dir;

    QElapsedTimer t; t.start();
    builder.start(exe, args);
    if (!builder.waitForStarted(5000)) {
        emitLog(QStringLiteral(
            "[wdsp] wisdom: BUILD FAILED — could not spawn "
            "subprocess: %1").arg(builder.errorString()));
        return false;
    }
    // 20 minute hard cap.  WDSP_PATIENT on a midrange machine
    // typically completes in 2-5 minutes; generous headroom for
    // slow CPUs but capped so a hung subprocess doesn't deadlock
    // launch.
    constexpr int kBuildTimeoutMs = 20 * 60 * 1000;
    if (!builder.waitForFinished(kBuildTimeoutMs)) {
        builder.kill();
        builder.waitForFinished(2000);
        emitLog(QStringLiteral(
            "[wdsp] wisdom: BUILD TIMEOUT after %1 min")
            .arg(kBuildTimeoutMs / 60000));
        return false;
    }
    if (builder.exitStatus() != QProcess::NormalExit ||
        builder.exitCode() != 0) {
        emitLog(QStringLiteral(
            "[wdsp] wisdom: BUILD FAILED — subprocess exit %1")
            .arg(builder.exitCode()));
        return false;
    }
    if (!wisdomFileExists(dir)) {
        emitLog(QStringLiteral(
            "[wdsp] wisdom: BUILD FAILED — subprocess succeeded "
            "but no wisdom file appeared at %1").arg(dir));
        return false;
    }
    emitLog(QStringLiteral(
        "[wdsp] wisdom: built in %1 s").arg(t.elapsed() / 1000));

    // Now do the in-process import of the freshly-built cache so
    // the rest of this process sees the plans.
    QString dirArg = dir;
    if (!dirArg.endsWith(QLatin1Char('/'))) {
        dirArg += QLatin1Char('/');
    }
    QByteArray dirBytes =
        QDir::toNativeSeparators(dirArg).toLocal8Bit();
    api_.WDSPwisdom(dirBytes.data());
    emitLog(QStringLiteral("[wdsp] wisdom: loaded from %1").arg(dir));
    return true;
}

} // namespace lyra::dsp
