// Lyra — WDSP DLL loader (Step 3a).
//
// Loads `wdsp.dll` (and its dependent `libfftw3-3.dll` /
// `libfftw3f-3.dll` / `rnnoise.dll` / `specbleach.dll`) from
// `<lyra.exe dir>/_native/` at application startup.  Step 3a's
// scope is ONLY load-or-fail — no function pointers resolved yet
// (that's Step 3b), no DSP yet (Step 3c+).
//
// The DLLs are GPL v3+ (NR0V's WDSP DSP engine + FFTW dependencies)
// and are LINK-COMPATIBLE with Lyra (also GPL v3+).  Per the locked
// architecture rule from FEATURES.md §0 they are LINKED DIRECTLY
// into the binary at runtime via LoadLibrary (the cffi pivot that
// rescued the Python tree from per-sample Python/GIL overhead now
// happens natively in C++ — no cffi, no GIL, no Python anywhere).
//
// Why explicit dynamic-load instead of implicit link:
//   * We don't have an `wdsp.lib` import library from the Python
//     tree — only the DLLs.  Generating one via `dumpbin /exports`
//     + `lib /def:` is doable but adds toolchain complexity for
//     zero functional benefit.
//   * Explicit LoadLibrary + GetProcAddress (Step 3b) keeps the
//     binding boundary explicit + searchable in code.
//   * Matches the proven Python tree pattern (cffi `dlopen` →
//     `ffi.cdef`) line-for-line: declare the C ABI ourselves,
//     resolve symbols at runtime.

#pragma once

#include <QObject>
#include <QString>

namespace lyra::dsp {

class WdspNative : public QObject {
    Q_OBJECT
    // Exposed to QML as a context property so the operator can
    // SEE the load status in the UI (Step 3a polish — log line in
    // the existing log panel; richer surfacing lands later).
    Q_PROPERTY(bool    loaded     READ isLoaded     NOTIFY loadedChanged)
    Q_PROPERTY(QString loadedFrom READ loadedFrom   NOTIFY loadedChanged)
    Q_PROPERTY(QString loadError  READ loadError    NOTIFY loadedChanged)

public:
    explicit WdspNative(QObject *parent = nullptr);
    ~WdspNative() override;

    bool    isLoaded()   const { return handle_ != nullptr; }
    QString loadedFrom() const { return loadedFrom_; }
    QString loadError()  const { return loadError_; }

    // Attempt to load wdsp.dll from `<lyra.exe directory>/_native/`.
    // Idempotent: subsequent calls after success are no-ops.  Safe
    // to call before main window construction.  Returns true on
    // success, false on failure (operator sees `loadError` for the
    // Windows error message).
    bool load();

    // Force-unload (testing / shutdown).  Generally we let the OS
    // do it at process exit.
    void unload();

signals:
    void loadedChanged();
    void logLine(QString line);

private:
    // We deliberately keep this as a `void*` so the header doesn't
    // drag windows.h through every translation unit that includes
    // it.  Cast to HMODULE in the cpp.
    void   *handle_ = nullptr;
    QString loadedFrom_;
    QString loadError_;
};

} // namespace lyra::dsp
