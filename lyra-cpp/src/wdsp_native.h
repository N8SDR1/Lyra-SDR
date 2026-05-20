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

// WDSP C ABI function pointer types.  All extern "C" -- no name
// mangling, matching the wdsp.dll exports directly.  Signatures
// verified against the upstream WDSP source (Warren Pratt NR0V,
// GPL v3+).  Parameter types are LOAD-BEARING -- a single
// `int` vs `double` mismatch on Windows x86_64 causes a
// register-class calling-convention bug (cf. the v0.0.9.8.1
// SetRXAAGCSlope regression in the Python tree, CLAUDE.md
// §15.26).  Do NOT modify these without re-verifying.
extern "C" {
using fn_OpenChannel_t = void (*)(
    int channel, int in_size, int dsp_size,
    int input_samplerate, int dsp_rate, int output_samplerate,
    int type, int state,
    double tdelayup, double tslewup,
    double tdelaydown, double tslewdown,
    int block);
using fn_CloseChannel_t        = void (*)(int channel);
using fn_SetChannelState_t     = int  (*)(int channel, int state, int dmode);
using fn_fexchange0_t          = void (*)(int channel,
                                          double *in_buff,
                                          double *out_buff,
                                          int *error);
using fn_SetRXAMode_t          = void (*)(int channel, int mode);
using fn_RXASetPassband_t      = void (*)(int channel,
                                          double f_low, double f_high);
using fn_SetRXAAGCMode_t       = void (*)(int channel, int mode);
using fn_SetRXAPanelBinaural_t = void (*)(int channel, int bin);
using fn_WDSPwisdom_t          = int  (*)(char *directory);
} // extern "C"

// Resolved function pointers.  Step 3b populates these via
// GetProcAddress at load() time; nullptr until then.  Step 3c+
// reads them via WdspNative::api().
struct WdspApi {
    fn_OpenChannel_t         OpenChannel         = nullptr;
    fn_CloseChannel_t        CloseChannel        = nullptr;
    fn_SetChannelState_t     SetChannelState     = nullptr;
    fn_fexchange0_t          fexchange0          = nullptr;
    fn_SetRXAMode_t          SetRXAMode          = nullptr;
    fn_RXASetPassband_t      RXASetPassband      = nullptr;
    fn_SetRXAAGCMode_t       SetRXAAGCMode       = nullptr;
    fn_SetRXAPanelBinaural_t SetRXAPanelBinaural = nullptr;
    fn_WDSPwisdom_t          WDSPwisdom          = nullptr;
};

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

    // Access the resolved function-pointer table.  Step 3c+ uses
    // this via `wdsp.api().OpenChannel(...)` etc.  All pointers
    // are nullptr until load() succeeds.
    const WdspApi &api() const { return api_; }

signals:
    void loadedChanged();
    void logLine(QString line);

private:
    bool resolveSymbols();
    void emitLog(const QString &line);   // mirror logLine -> qInfo console

    // We deliberately keep this as a `void*` so the header doesn't
    // drag windows.h through every translation unit that includes
    // it.  Cast to HMODULE in the cpp.
    void   *handle_ = nullptr;
    QString loadedFrom_;
    QString loadError_;
    WdspApi api_;
};

} // namespace lyra::dsp
