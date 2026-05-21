// Lyra — WDSP RX channel engine (Step 3c-ii).
//
// Wraps a single WDSP receiver channel: OpenChannel + the locked
// first-light config (USB 200-3000 Hz, AGC MED, binaural mono) +
// SetChannelState start, with a matching close on teardown.
//
// Scope of Step 3c-ii is CHANNEL LIFECYCLE ONLY — open the channel,
// configure it, start it, prove it opens + closes without crashing.
// No IQ flows through fexchange0 yet (that's Step 3d), no audio is
// produced yet (Step 3e).
//
// Every parameter here is mirrored from the bench-proven Python tree
// (lyra/dsp/wdsp_engine.py RxConfig + RxChannel._open) so the C++
// rebuild starts from a known-good WDSP setup rather than re-deriving
// it.  See CLAUDE.md §14.2 for the load-bearing gotchas:
//   * OpenChannel 13th arg (block) MUST be 1 (block-until-output).
//   * out_size = in_size * out_rate / in_rate (NOT in_size).
//   * Sideband select lives in NBP0 — use RXASetPassband, not
//     SetRXABandpassFreqs (BP1 is bypassed with all DSP off).
//   * SetRXAPanelBinaural(ch, 0) => panel.copy=1 => mono on both
//     L/R; fixes the AM/FM/DSB right-channel-silent bug (§14.10).

#pragma once

#include "wdsp_native.h"

#include <QObject>
#include <QString>
#include <QTimer>

#include <atomic>
#include <functional>
#include <vector>

namespace lyra::dsp {

// Per-channel sample rates + buffer size.  Defaults match the working
// Thetis/Lyra HL2 setup: 1024-frame 192 kHz IQ in, 4096-sample
// internal DSP buffer at 48 kHz, 48 kHz audio out.
struct RxConfig {
    int    inSize     = 1024;     // frames per fexchange0 call
    int    dspSize    = 4096;     // internal DSP buffer size
    int    inRate     = 192000;   // IQ input rate (HL2 default)
    int    dspRate    = 48000;    // WDSP internal DSP rate
    int    outRate    = 48000;    // audio output rate (AK4951 fixed)
    // Slew envelope (avoids click on start/stop).
    double tDelayUp   = 0.010;
    double tSlewUp    = 0.025;
    double tDelayDown = 0.000;
    double tSlewDown  = 0.010;
    // 1 = fexchange0 blocks until the DSP thread has produced the next
    // output buffer.  Required for a steady cadence (§14.2).
    int    block      = 1;
};

class WdspEngine : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool   running   READ isRunning  NOTIFY runningChanged)
    // Step 3d: peak audio level out of the DSP chain, dBFS.  Sentinel
    // -200.0 = "no audio produced yet".  Written by the RX worker
    // thread in feedIq(); sampled by the UI at ~5 Hz via levelsChanged.
    Q_PROPERTY(double audioDbFs READ audioDbFs  NOTIFY levelsChanged)

public:
    explicit WdspEngine(WdspNative *wdsp, QObject *parent = nullptr);
    ~WdspEngine() override;

    bool isRunning() const { return running_; }

    double audioDbFs() const {
        return audioDbFs_.load(std::memory_order_relaxed);
    }

    // Frames fexchange0 writes per process() call (= in_size *
    // out_rate / in_rate).  Step 3d sizes its output buffer to this.
    int outSize() const { return outSize_; }

    // Step 3d: feed interleaved baseband IQ — (I,Q,I,Q,…) doubles
    // already normalized to [-1,1) — from the RX worker thread.
    // Accumulates into in_size blocks; each full block runs
    // fexchange0 IN-LINE on the caller's thread (block=1 returns as
    // soon as the DSP thread has the output ready, ~187 calls/sec)
    // and updates audioDbFs.  MUST be called from a SINGLE thread
    // (the HL2Stream RX worker) — not thread-safe within the channel.
    // No audio is played yet (Step 3e); this only measures.
    void feedIq(const double *iq, int nframes);

    // Open RX1 (channel 0), apply the locked first-light config and
    // start the channel.  Idempotent; returns true on success.
    Q_INVOKABLE bool openRx1();

    // Stop (blocking flush) + close channel 0.  Idempotent.  Called
    // automatically on destruction.
    Q_INVOKABLE void closeRx1();

signals:
    void runningChanged();
    void levelsChanged();
    void logLine(QString line);

private:
    void emitLog(const QString &line);   // mirror logLine -> qInfo console

    WdspNative *wdsp_    = nullptr;
    RxConfig    cfg_;
    int         channel_ = 0;
    int         outSize_ = 0;
    bool        opened_  = false;
    bool        running_ = false;

    // Step 3d DSP buffers (all sized in the constructor).
    // accum_ : interleaved IQ doubles awaiting a full in_size block.
    // outBuf_: fexchange0 output (2 * outSize_ doubles, L/R).
    std::vector<double> accum_;
    std::vector<double> outBuf_;
    int                 fexErr_ = 0;
    std::atomic<double> audioDbFs_{-200.0};
    // 5 Hz UI poll — emits levelsChanged so the QML audioDbFs binding
    // re-reads the atomic.  Lives on the main thread (WdspEngine is a
    // main-thread object); started in openRx1, stopped in closeRx1.
    QTimer              levelsTimer_;
};

} // namespace lyra::dsp
