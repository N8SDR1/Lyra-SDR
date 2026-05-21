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
// Step 3e level-cal: AGC threshold/slope + panel gain.  SetRXAAGCThresh
// computes the AGC max_gain from (thresh, size, rate) + the slope-
// derived var_gain — replacing WDSP's hot create-time default
// (max_gain = 10000 / 80 dB) that makes the audio overshoot 0 dBFS.
// SLOPE IS `int` — a `double` binding produces a register-class
// calling-convention bug on Windows x86_64 (CLAUDE.md §15.26 / the
// Python v0.0.9.8.1 SetRXAAGCSlope regression).  Do NOT change to double.
using fn_SetRXAAGCThresh_t     = void (*)(int channel, double thresh,
                                          double size, double rate);
using fn_SetRXAAGCSlope_t      = void (*)(int channel, int slope);
using fn_SetRXAPanelGain1_t    = void (*)(int channel, double gain);

// Step 5: WDSP spectral analyzer (panadapter source).  Same pipeline
// Thetis uses — XCreateAnalyzer + SetAnalyzer to configure, Spectrum0
// to feed IQ (interleaved doubles, like fexchange0), GetPixels to
// retrieve a display-width dB array.  Signatures verified against
// wdsp/analyzer.h.  GetPixels writes FLOAT pixels (dOUTREAL=float);
// Spectrum0 takes DOUBLE IQ.  Do NOT swap those.
using fn_XCreateAnalyzer_t        = void (*)(int disp, int *success,
                                             int m_size, int m_LO,
                                             int m_stitch,
                                             char *app_data_path);
using fn_DestroyAnalyzer_t        = void (*)(int disp);
// SIGNATURE VERIFIED against wdsp/analyzer.c:1189 (the C# param NAMES
// in PanDisplay.cs are misleading): param 5 `flp` is an int* vector
// (one high-side-LO flag per FFT), NOT an int — passing an int there
// is a null-pointer deref crash.  fsc_lin/fsc_hin are DOUBLE, not int.
using fn_SetAnalyzer_t            = void (*)(int disp, int n_pixout,
                                             int n_fft, int typ,
                                             int *flp, int sz, int bf_sz,
                                             int win_type, double pi,
                                             int ovrlp, int clp,
                                             double fsc_lin, double fsc_hin,
                                             int n_pix, int n_stch,
                                             int calset, double fmin,
                                             double fmax, int max_w);
using fn_Spectrum0_t              = void (*)(int run, int disp, int ss,
                                             int LO, double *pbuff);
using fn_GetPixels_t              = void (*)(int disp, int pixout,
                                             float *pix, int *flag,
                                             double *pixel_ref);
using fn_SetDisplayDetectorMode_t = void (*)(int disp, int pixout, int mode);
using fn_SetDisplayAverageMode_t  = void (*)(int disp, int pixout, int mode);
using fn_SetDisplayNumAverage_t   = void (*)(int disp, int pixout, int num);
// Exponential back-multiplier for recursive display averaging.  Paired
// with SetDisplayNumAverage; both derived from a time constant tau:
//   mult = exp(-1 / (frame_rate * tau)),  num = frame_rate * tau.
using fn_SetDisplayAvBackmult_t   = void (*)(int disp, int pixout,
                                             double mult);
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
    fn_SetRXAAGCThresh_t     SetRXAAGCThresh     = nullptr;
    fn_SetRXAAGCSlope_t      SetRXAAGCSlope      = nullptr;
    fn_SetRXAPanelGain1_t    SetRXAPanelGain1    = nullptr;
    // Step 5: spectral analyzer (panadapter).
    fn_XCreateAnalyzer_t        XCreateAnalyzer        = nullptr;
    fn_DestroyAnalyzer_t        DestroyAnalyzer        = nullptr;
    fn_SetAnalyzer_t            SetAnalyzer            = nullptr;
    fn_Spectrum0_t              Spectrum0              = nullptr;
    fn_GetPixels_t              GetPixels              = nullptr;
    fn_SetDisplayDetectorMode_t SetDisplayDetectorMode = nullptr;
    fn_SetDisplayAverageMode_t  SetDisplayAverageMode  = nullptr;
    fn_SetDisplayNumAverage_t   SetDisplayNumAverage   = nullptr;
    fn_SetDisplayAvBackmult_t   SetDisplayAvBackmult   = nullptr;
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

    // Step 3c-i: FFTW WISDOM plumbing.  WDSP plans every internal
    // FFT at FFTW_PATIENT (the most expensive planner tier).  With
    // no cached wisdom file, FFTW_PATIENT re-runs in-process on
    // every OpenChannel — multi-minute stall, with the UI frozen.
    // ensureWisdom() resolves that by:
    //
    //   (a) computing Lyra-C++-PRIVATE wisdom dir
    //       %APPDATA%\N8SDR\Lyra-cpp\fftw\  — note the `-cpp` suffix
    //       so we never share with Python Lyra (which uses .../Lyra/
    //       fftw/, leave it untouched per CLAUDE.md §15.26
    //       isolation-by-directory rule);
    //
    //   (b) if `wdspWisdom00` exists in that dir, calling
    //       api().WDSPwisdom(<dir>) IN-PROCESS — fast import,
    //       <100 ms typical;
    //
    //   (c) if it does not, spawning `lyra.exe --build-wisdom <dir>`
    //       as a SUBPROCESS — the subprocess does the multi-minute
    //       FFTW_PATIENT search and writes the file, then exits;
    //       the main process waits + then loads the cached result.
    //       Subprocess isolation is mandatory because WDSPwisdom()
    //       internally calls AllocConsole() + FreeConsole() which
    //       hijacks stdout when run in a `--windowed` Qt app
    //       (operator-bench-verified bite in the Python tree,
    //       CLAUDE.md §15.26 commit f936b2e).
    //
    // Returns true on success.  Status messages stream through
    // emitLog() (console + QML log panel).
    bool ensureWisdom();

    // Invoked by main.cpp when argv[1] == "--build-wisdom".  Runs
    // ONLY the wisdom build in this process, then exits with code
    // 0 (built ok) / 1 (no DLL) / 2 (no dir).  The parent process
    // launched us with stdio piped to DEVNULL; AllocConsole's
    // hijack is harmless because we never produce any output that
    // anyone reads.
    int runWisdomBuilderEntryPoint(const QString &targetDir);

    // Returns the path Lyra C++ uses for the wisdom cache.  Public
    // so test code / future Settings UI can read it.
    static QString wisdomDir();

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
