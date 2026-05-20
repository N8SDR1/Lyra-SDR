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
#include <QDir>
#include <QFileInfo>
#include <QString>

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
        emit logLine(QStringLiteral("[wdsp] LOAD FAILED: %1")
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
        emit logLine(QStringLiteral(
            "[wdsp] LOAD FAILED: LoadLibraryExW(%1): %2")
            .arg(dllPath, loadError_));
        emit loadedChanged();
        return false;
    }

    handle_     = static_cast<void*>(h);
    loadedFrom_ = dllPath;
    loadError_.clear();
    emit logLine(QStringLiteral("[wdsp] LOADED: %1").arg(dllPath));
    emit loadedChanged();
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

} // namespace lyra::dsp
