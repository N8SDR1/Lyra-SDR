// Lyra — WDSP RX channel engine implementation (Step 3c-ii).
// See wdsp_engine.h for the locked scope + the §14.2 gotcha list.

#include "wdsp_engine.h"

#include <QAudioDevice>
#include <QAudioFormat>
#include <QAudioSink>
#include <QDebug>
#include <QIODevice>
#include <QMediaDevices>
#include <QSettings>

#include <algorithm>
#include <cmath>
#include <mutex>

namespace lyra::dsp {

namespace {

// Mode / AGC integer constants, verified against the Python tree's
// wdsp_native.py (RxaMode / AgcMode) which in turn matches the
// upstream WDSP source.  Do NOT change without re-verifying — these
// map directly onto WDSP's RXA mode + wcpAGC mode switch statements.
constexpr int    kRxaModeUSB = 1;   // RxaMode.USB
constexpr int    kAgcModeMed = 3;   // AgcMode.MED
constexpr double kUsbLowHz   = 200.0;
constexpr double kUsbHighHz  = 3000.0;

// Step 3e level-cal (mirrors the bench-proven Python _open_wdsp_rx).
// slope 35 -> var_gain = 10^(35/200) ~ 1.5 (WDSP/industry soft-knee);
// threshold -100 dBFS gives the AGC ~70 dB of headroom to boost weak
// signals without over-driving the output past 0 dBFS.  size 4096 +
// the IQ input rate are SetRXAAGCThresh's noise-bandwidth conversion
// args.  Do NOT also call SetRXAAGCTop — it writes the same max_gain
// field SetRXAAGCThresh computes and would clobber it.
constexpr int    kAgcSlope         = 35;
constexpr double kAgcThreshDbFs    = -100.0;
constexpr double kAgcThreshFftSize = 4096.0;

// Step 5: WDSP spectral analyzer (panadapter) config.  Values mirror
// Thetis's HL2 RX analyzer (fft 4096, window 4, kaiser 14).  pixels is
// fixed (the panadapter scales it to its on-screen width).  overlap =
// fft - feed-block so one FFT advances per IQ feed (smooth).  Detector
// peak + recursive time-average for a stable display.  All tunable on
// the bench.
constexpr int    kAnDisp        = 0;       // analyzer id (RX1)
constexpr int    kAnMaxFft      = 32768;   // max fft (must be >= max_w)
constexpr int    kAnFftSize     = 4096;
constexpr int    kAnPixels      = 2048;    // dB output points
constexpr int    kAnWindow      = 4;       // soft-knee window (ref default)
constexpr double kAnKaiserPi    = 14.0;
constexpr int    kAnFrameRate   = 60;      // display fps — drives overlap,
                                           // max_w + averaging frame count.
                                           // Higher = more responsive trace
                                           // (tau holds the smoothing TIME
                                           // constant).  Operator-adjustable
                                           // control to come (Thetis-style).
constexpr int    kAnDetector    = 0;       // 0 = peak
// Time-averaging OFF by default (reference ships AverageOn=false) — a
// LIVE trace, no smoothing lag, so it stays in sync with the audio.
// The smooth "fluid" look comes from the curve RENDERING (no lag), not
// from time-averaging.  Flip kAnAvgMode != 0 + tune kAnTau for an
// operator AVG toggle later.
constexpr int    kAnAvgMode     = 0;       // 0 = off (live)
constexpr double kAnTau         = 0.12;    // avg time constant (s) when on

// Step 3e audio-ring sizing.  The reference Python SoundDeviceSink
// pre-fills 100 ms (= 4800 frames @ 48 kHz) because Python/GIL
// delivered audio in bursty ~43 ms lumps that drained the ring.  The
// C++ RX worker pushes audio every ~5.3 ms with no GIL, so we run a
// SMALLER pre-fill.  Both tunable here.
constexpr int    kRingMs       = 200;  // ring capacity (ms)
constexpr int    kPrefillMs    = 30;   // startup silence pre-fill (ms)
// Explicit QAudioSink device-buffer depth.  Qt's default is large
// (~100-200 ms on WASAPI) and was the dominant audio latency that made
// the audio lag the panadapter.  ~40 ms keeps it tight without
// underrunning (operator-tunable on the bench).
constexpr int    kSinkBufferMs = 40;

// Step 3e: perceptual volume taper.  Slider position (0..1) -> dB gain
// so comfortable listening sits mid-slider instead of bunched at the
// bottom (a linear gain made 8% already loud).  pos=1 -> 0 dB (unity),
// pos=0.5 -> -20 dB, pos->0 -> silence.  Floor -40 dB (was -60) so the
// whole curve is louder per position (operator: "10 should sound like
// 20").  Mirrors the Python dB-volume idiom (CLAUDE.md §15.17).
constexpr double kMinVolDb = -40.0;

double posToDb(double pos) {
    return (pos <= 0.0) ? kMinVolDb : kMinVolDb * (1.0 - pos);
}

double posToGain(double pos) {
    if (pos <= 0.0) return 0.0;
    if (pos >= 1.0) return 1.0;
    return std::pow(10.0, posToDb(pos) / 20.0);
}

} // namespace

// ---------------------------------------------------------------
// AudioRing — the QIODevice the QAudioSink pulls from (pull mode,
// the Qt-native equivalent of the reference's CallbackASIO /
// PortAudio callback).  Producer = the RX worker thread via push();
// consumer = Qt's audio backend thread via readData().  A std::mutex
// guards the ring indices (hold time = a memcpy of a few hundred
// int16, sub-ms).  On underrun readData pads silence and always
// returns the full requested length so the sink never stops.
// ---------------------------------------------------------------
class AudioRing : public QIODevice {
public:
    explicit AudioRing(int capacityFrames)
        : capFrames_(capacityFrames),
          buf_(static_cast<size_t>(capacityFrames) * 2, 0) {}

    // Producer (RX worker thread): push interleaved stereo int16.
    void push(const qint16 *lr, int nframes) {
        std::lock_guard<std::mutex> lk(mtx_);
        for (int f = 0; f < nframes; ++f) {
            if (countFrames_ >= capFrames_) {
                rd_ = (rd_ + 1) % capFrames_;   // overrun: drop oldest
                --countFrames_;
                ++overruns_;
            }
            buf_[static_cast<size_t>(wr_) * 2 + 0] = lr[f * 2 + 0];
            buf_[static_cast<size_t>(wr_) * 2 + 1] = lr[f * 2 + 1];
            wr_ = (wr_ + 1) % capFrames_;
            ++countFrames_;
        }
    }

    // Seed the ring with `frames` of silence so the sink has headroom
    // before the worker produces its first block.
    void prefillSilence(int frames) {
        std::lock_guard<std::mutex> lk(mtx_);
        rd_ = wr_ = countFrames_ = 0;
        const int n = std::min(frames, capFrames_);
        for (int i = 0; i < n; ++i) {
            buf_[static_cast<size_t>(wr_) * 2 + 0] = 0;
            buf_[static_cast<size_t>(wr_) * 2 + 1] = 0;
            wr_ = (wr_ + 1) % capFrames_;
            ++countFrames_;
        }
    }

    qint64 overruns()  const { std::lock_guard<std::mutex> lk(mtx_); return overruns_;  }
    qint64 underruns() const { std::lock_guard<std::mutex> lk(mtx_); return underruns_; }

    // --- QIODevice overrides (no Q_OBJECT needed: only virtuals) ---
    bool   isSequential() const override { return true; }
    qint64 writeData(const char *, qint64) override { return -1; }

    // Report a large, always-positive availability so the sink keeps
    // pulling on its own cadence (we pad silence inside readData).
    qint64 bytesAvailable() const override {
        return static_cast<qint64>(capFrames_) * kBytesPerFrame
               + QIODevice::bytesAvailable();
    }

    qint64 readData(char *data, qint64 maxlen) override {
        const qint64 maxFrames = maxlen / kBytesPerFrame;
        if (maxFrames <= 0) {
            return 0;
        }
        qint16 *out = reinterpret_cast<qint16 *>(data);
        bool padded = false;
        std::lock_guard<std::mutex> lk(mtx_);
        for (qint64 f = 0; f < maxFrames; ++f) {
            if (countFrames_ > 0) {
                out[f * 2 + 0] = buf_[static_cast<size_t>(rd_) * 2 + 0];
                out[f * 2 + 1] = buf_[static_cast<size_t>(rd_) * 2 + 1];
                rd_ = (rd_ + 1) % capFrames_;
                --countFrames_;
            } else {
                out[f * 2 + 0] = 0;   // underrun -> silence
                out[f * 2 + 1] = 0;
                padded = true;
            }
        }
        if (padded) {
            ++underruns_;
        }
        return maxFrames * kBytesPerFrame;   // always full -> sink stays active
    }

private:
    static constexpr qint64 kBytesPerFrame = 2 * sizeof(qint16);  // stereo
    mutable std::mutex  mtx_;
    std::vector<qint16> buf_;        // interleaved L,R; capFrames_*2
    int                 capFrames_;
    int                 rd_ = 0, wr_ = 0, countFrames_ = 0;
    qint64              overruns_ = 0, underruns_ = 0;
};

WdspEngine::WdspEngine(WdspNative *wdsp, QObject *parent)
    : QObject(parent), wdsp_(wdsp)
{
    // out_size = in_size * out_rate / in_rate (when in_rate >= out_rate).
    // With 1024 @ 192k -> 48k that is 256 frames per fexchange0 call.
    if (cfg_.inRate >= cfg_.outRate) {
        outSize_ = cfg_.inSize / (cfg_.inRate / cfg_.outRate);
    } else {
        outSize_ = cfg_.inSize * (cfg_.outRate / cfg_.inRate);
    }

    // fexchange0 output buffer: 2 * outSize_ doubles (interleaved L/R).
    outBuf_.assign(static_cast<size_t>(2 * outSize_), 0.0);
    // int16 stereo scratch for the audio-ring push (Step 3e).
    pcm16_.assign(static_cast<size_t>(2 * outSize_), 0);
    // Headroom for one in_size block + a couple of EP6 datagrams so
    // feedIq's append never reallocates in steady state.
    accum_.reserve(static_cast<size_t>(2 * (cfg_.inSize + 128)));

    // 5 Hz UI poll: emit levelsChanged so the QML audioDbFs binding
    // re-reads the atomic (mirrors HL2Stream's statsTimer cadence).
    levelsTimer_.setInterval(200);
    connect(&levelsTimer_, &QTimer::timeout,
            this, &WdspEngine::levelsChanged);

    // Step 3e: enumerate the operator's PC output devices + default.
    devices_ = QMediaDevices::audioOutputs();
    const QAudioDevice def = QMediaDevices::defaultAudioOutput();
    for (int i = 0; i < devices_.size(); ++i) {
        if (devices_[i].id() == def.id()) {
            deviceIndex_ = i;
            break;
        }
    }

    // Radio memory: restore the operator's volume + output device.
    // muted_ is DELIBERATELY NOT restored — we always start muted so
    // auto-connect can never auto-blast (operator speaker-damage
    // history).  Device matched by description; falls back to the
    // system default (set above) if it's no longer present.
    QSettings s;
    volume_.store(std::clamp(
        s.value(QStringLiteral("audio/volume"), 0.65).toDouble(), 0.0, 1.0),
        std::memory_order_relaxed);
    const QString savedDev =
        s.value(QStringLiteral("audio/deviceName")).toString();
    if (!savedDev.isEmpty()) {
        for (int i = 0; i < devices_.size(); ++i) {
            if (devices_[i].description() == savedDev) {
                deviceIndex_ = i;
                break;
            }
        }
    }
}

WdspEngine::~WdspEngine()
{
    closeRx1();
}

void WdspEngine::emitLog(const QString &line)
{
    qInfo("%s", qPrintable(line));
    emit logLine(line);
}

bool WdspEngine::openRx1()
{
    if (opened_) {
        return true;  // idempotent
    }
    if (!wdsp_ || !wdsp_->isLoaded()) {
        emitLog(QStringLiteral(
            "[wdsp] engine: cannot open RX1 — DLL not loaded"));
        return false;
    }

    const WdspApi &api = wdsp_->api();
    if (!api.OpenChannel || !api.SetChannelState || !api.SetRXAMode ||
        !api.RXASetPassband || !api.SetRXAAGCMode ||
        !api.SetRXAPanelBinaural) {
        emitLog(QStringLiteral(
            "[wdsp] engine: cannot open RX1 — required symbols not "
            "resolved"));
        return false;
    }

    // OpenChannel(channel, in_size, dsp_size, in_rate, dsp_rate,
    //             out_rate, type=RX(0), state=stopped(0),
    //             tdelayup, tslewup, tdelaydown, tslewdown, block).
    api.OpenChannel(channel_, cfg_.inSize, cfg_.dspSize,
                    cfg_.inRate, cfg_.dspRate, cfg_.outRate,
                    0,   // type = RX
                    0,   // state = stopped — we start it explicitly below
                    cfg_.tDelayUp, cfg_.tSlewUp,
                    cfg_.tDelayDown, cfg_.tSlewDown,
                    cfg_.block);
    opened_ = true;

    // Binaural OFF (arg 0) => WDSP panel.copy=1 => I copied to Q at the
    // panel output => mono on BOTH L/R regardless of any upstream stage
    // that zeroed Q (e.g. EMNR).  This is the Thetis default + the
    // AM/FM/DSB right-channel-silent fix (CLAUDE.md §14.10).
    api.SetRXAPanelBinaural(channel_, 0);

    // USB demod.
    api.SetRXAMode(channel_, kRxaModeUSB);

    // SSB passband.  RXASetPassband updates NBP0 (front-of-chain, where
    // sideband selection lives + always runs) + BP1 + the SNBA filter
    // in one call.  SetRXABandpassFreqs alone would only touch BP1,
    // which is bypassed with all DSP off (§14.2).
    api.RXASetPassband(channel_, kUsbLowHz, kUsbHighHz);

    // AGC medium.
    api.SetRXAAGCMode(channel_, kAgcModeMed);

    // Level calibration: replace WDSP's hot create-time AGC default
    // (max_gain = 10000 / 80 dB, which overshoots 0 dBFS) with a
    // threshold-computed ceiling.  SetRXAAGCThresh derives max_gain
    // from (thresh, size, rate) + the slope-derived var_gain; we must
    // NOT also call SetRXAAGCTop (same field, would clobber).
    if (api.SetRXAAGCSlope) {
        api.SetRXAAGCSlope(channel_, kAgcSlope);
    }
    if (api.SetRXAAGCThresh) {
        api.SetRXAAGCThresh(channel_, kAgcThreshDbFs, kAgcThreshFftSize,
                            static_cast<double>(cfg_.inRate));
    }
    // Panel (post-DSP makeup) gain at unity — Thetis default.
    if (api.SetRXAPanelGain1) {
        api.SetRXAPanelGain1(channel_, 1.0);
    }

    // Step 5: create + configure the spectral analyzer (panadapter
    // source).  Independent of the DSP channel; fed the same IQ.
    if (api.XCreateAnalyzer && api.SetAnalyzer) {
        int success = 0;
        char appDataPath[] = "";   // empty app-data path (no temp files)
        api.XCreateAnalyzer(kAnDisp, &success, kAnMaxFft, 1, 1, appDataPath);
        if (success == 0) {
            // overlap + max_w per the frame-rate formula.  max_w sizes
            // an internal display-history buffer — passing 0 makes WDSP
            // crash on a zero-size allocation.  overlap clamps to 0 at
            // 192 kHz / 4096 (samples-per-frame >> fft).
            const int overlap =
                std::max(0, kAnFftSize - cfg_.inRate / kAnFrameRate);
            const int maxW = kAnFftSize +
                std::min(cfg_.inRate / 10, kAnFftSize * kAnFrameRate / 10);
            // flp = per-FFT high-side-LO flags (int* vector).  One FFT,
            // not high-side -> {0}.  MUST be a real pointer (passing an
            // int crashes WDSP — it dereferences flp[i]).
            int flp[1] = {0};
            api.SetAnalyzer(
                kAnDisp,                    // disp
                1,                          // n_pixout
                1,                          // n_fft (spur-elim ffts)
                1,                          // typ = complex IQ
                flp,                        // flp (int* high-side LO flags)
                kAnFftSize,                 // sz (fft size)
                cfg_.inSize,                // bf_sz (our feed block)
                kAnWindow,                  // win_type
                kAnKaiserPi,                // pi (kaiser)
                overlap,                    // ovrlp
                0,                          // clp (clip bins/side)
                0.0, 0.0,                   // fsc_lin, fsc_hin (DOUBLE)
                kAnPixels,                  // n_pix (display points)
                1,                          // n_stch (stitches)
                0,                          // calset
                0.0, 0.0,                   // fmin, fmax
                maxW);                      // max_w (history buffer size)
            if (api.SetDisplayDetectorMode) {
                api.SetDisplayDetectorMode(kAnDisp, 0, kAnDetector);
            }
            // Average mode (0 = off = live trace by default).  When
            // enabled, the back-multiplier + frame count derive from
            // tau + frame_rate exactly as the reference does.
            if (api.SetDisplayAverageMode) {
                api.SetDisplayAverageMode(kAnDisp, 0, kAnAvgMode);
            }
            if (kAnAvgMode != 0) {
                const double avb =
                    std::exp(-1.0 / (kAnFrameRate * kAnTau));
                const int numAvg = std::max(2,
                    std::min(60, static_cast<int>(kAnFrameRate * kAnTau)));
                if (api.SetDisplayAvBackmult) {
                    api.SetDisplayAvBackmult(kAnDisp, 0, avb);
                }
                if (api.SetDisplayNumAverage) {
                    api.SetDisplayNumAverage(kAnDisp, 0, numAvg);
                }
            }
            analyzerOpen_ = true;
            emitLog(QStringLiteral(
                "[wdsp] analyzer: %1 pixels, fft %2, window %3 "
                "(panadapter source)")
                .arg(kAnPixels).arg(kAnFftSize).arg(kAnWindow));
        } else {
            emitLog(QStringLiteral(
                "[wdsp] analyzer: XCreateAnalyzer failed (%1) — no "
                "panadapter").arg(success));
        }
    }

    // Step 3e: bring up PC sound-card playback BEFORE running_ goes
    // true, so feedIq sees a live ring the moment IQ starts flowing.
    // Non-fatal: if the audio device is missing/unsupported we log and
    // continue (the dBFS meter still works, just no sound).
    startAudio();

    // Start the channel (state=running, dmode=0).
    api.SetChannelState(channel_, 1, 0);
    running_ = true;
    emit runningChanged();
    levelsTimer_.start();   // begin the 5 Hz audioDbFs UI poll

    emitLog(QStringLiteral(
        "[wdsp] channel 0 opened (192k IQ -> 48k audio, USB "
        "200-3000 Hz, AGC MED thr -100 dBFS, binaural mono); "
        "out_size=%1 frames").arg(outSize_));
    return true;
}

void WdspEngine::closeRx1()
{
    if (!opened_) {
        return;  // idempotent
    }
    const WdspApi &api = wdsp_->api();
    // Stop with dmode=1 (blocking flush) so in-flight buffers drain
    // before CloseChannel tears the channel down.
    if (api.SetChannelState) {
        api.SetChannelState(channel_, 0, 1);
    }
    if (api.CloseChannel) {
        api.CloseChannel(channel_);
    }
    opened_ = false;
    if (running_) {
        running_ = false;
        emit runningChanged();
    }
    levelsTimer_.stop();
    stopAudio();
    if (analyzerOpen_ && api.DestroyAnalyzer) {
        api.DestroyAnalyzer(kAnDisp);
        analyzerOpen_ = false;
    }
    audioDbFs_.store(-200.0, std::memory_order_relaxed);
    accum_.clear();
    emitLog(QStringLiteral("[wdsp] channel 0 closed"));
}

int WdspEngine::spectrumPixelCount() const
{
    return kAnPixels;
}

int WdspEngine::copySpectrum(float *dst, int maxN)
{
    if (!analyzerOpen_ || dst == nullptr) {
        return 0;
    }
    const WdspApi &api = wdsp_->api();
    if (!api.GetPixels) {
        return 0;
    }
    const int n = std::min(maxN, kAnPixels);
    int    flag = 0;
    double ref  = 0.0;
    // GetPixels writes kAnPixels floats (dB) into dst.  WDSP serialises
    // this against the RX-worker Spectrum0 feed internally.
    api.GetPixels(kAnDisp, 0, dst, &flag, &ref);
    return n;
}

bool WdspEngine::startAudio()
{
    QAudioFormat fmt;
    fmt.setSampleRate(cfg_.outRate);
    fmt.setChannelCount(2);
    fmt.setSampleFormat(QAudioFormat::Int16);

    if (devices_.isEmpty() ||
        deviceIndex_ < 0 || deviceIndex_ >= devices_.size()) {
        emitLog(QStringLiteral(
            "[wdsp] audio: no output device — playback disabled "
            "(dBFS meter still live)"));
        return false;
    }
    const QAudioDevice dev = devices_[deviceIndex_];
    if (!dev.isFormatSupported(fmt)) {
        emitLog(QStringLiteral(
            "[wdsp] audio: '%1' does not support 48 kHz/stereo/int16 — "
            "playback disabled (dBFS meter still live)")
            .arg(dev.description()));
        return false;
    }

    const int capFrames     = cfg_.outRate * kRingMs    / 1000;
    const int prefillFrames = cfg_.outRate * kPrefillMs / 1000;
    {
        std::lock_guard<std::mutex> lk(audioMtx_);
        if (audioSink_) {
            return true;  // already up
        }
        audioRing_ = new AudioRing(capFrames);
        audioRing_->prefillSilence(prefillFrames);
        audioRing_->open(QIODevice::ReadOnly);

        audioSink_ = new QAudioSink(dev, fmt, this);
        // Cap the device buffer so audio latency stays low (else Qt's
        // large default makes audio lag the panadapter).  Must be set
        // before start().
        audioSink_->setBufferSize(
            cfg_.outRate * kSinkBufferMs / 1000 * 2 *
            static_cast<int>(sizeof(qint16)));
        audioSink_->start(audioRing_);   // pull mode
    }

    emitLog(QStringLiteral(
        "[wdsp] audio: '%1' @ 48 kHz stereo int16 (ring %2 ms, "
        "prefill %3 ms) — MUTED, volume %4 dB")
        .arg(dev.description()).arg(kRingMs).arg(kPrefillMs)
        .arg(static_cast<int>(posToDb(volume_.load()))));
    return true;
}

void WdspEngine::stopAudio()
{
    // sink->stop() quiesces Qt's audio thread (no more readData) before
    // we delete the ring; audioMtx_ blocks the RX worker's push().
    std::lock_guard<std::mutex> lk(audioMtx_);
    if (audioSink_) {
        audioSink_->stop();
        delete audioSink_;
        audioSink_ = nullptr;
    }
    if (audioRing_) {
        audioRing_->close();
        delete audioRing_;
        audioRing_ = nullptr;
    }
}

double WdspEngine::volumeDb() const
{
    return posToDb(volume_.load(std::memory_order_relaxed));
}

void WdspEngine::setVolume(double v)
{
    v = std::clamp(v, 0.0, 1.0);
    volume_.store(v, std::memory_order_relaxed);
    QSettings().setValue(QStringLiteral("audio/volume"), v);
    emit volumeChanged();
}

void WdspEngine::setMuted(bool m)
{
    muted_.store(m, std::memory_order_relaxed);
    emit mutedChanged();
    emitLog(m ? QStringLiteral("[wdsp] audio: muted")
              : QStringLiteral("[wdsp] audio: unmuted (volume %1 dB)")
                    .arg(static_cast<int>(posToDb(volume_.load()))));
}

QStringList WdspEngine::audioOutputDevices() const
{
    QStringList names;
    for (const QAudioDevice &d : devices_) {
        names << d.description();
    }
    return names;
}

void WdspEngine::setAudioOutputDevice(int index)
{
    if (index < 0 || index >= devices_.size() || index == deviceIndex_) {
        return;
    }
    deviceIndex_ = index;
    QSettings().setValue(QStringLiteral("audio/deviceName"),
                         devices_[index].description());
    emit audioDeviceChanged();
    emitLog(QStringLiteral("[wdsp] audio: output device -> %1")
                .arg(devices_[index].description()));
    // Live switch: tear the sink down and rebuild on the new device.
    // (stopAudio/startAudio each take audioMtx_; no nesting here.)
    if (running_) {
        stopAudio();
        startAudio();
    }
}

void WdspEngine::feedIq(const double *iq, int nframes)
{
    // Drop IQ until the channel is live (e.g. samples arriving in the
    // window between stream-open and the deferred openRx1, or after a
    // close).  fexchange0 on a closed channel is undefined.
    if (!running_ || nframes <= 0) {
        return;
    }
    const WdspApi &api = wdsp_->api();
    if (!api.fexchange0) {
        return;
    }

    // Append this datagram's interleaved IQ to the accumulator.
    accum_.insert(accum_.end(), iq,
                  iq + static_cast<size_t>(2 * nframes));

    // Drain whole in_size blocks through WDSP.  in_size frames =
    // 2*in_size interleaved doubles in, 2*outSize_ doubles out.
    const size_t blockDoubles = static_cast<size_t>(2 * cfg_.inSize);
    while (accum_.size() >= blockDoubles) {
        api.fexchange0(channel_, accum_.data(), outBuf_.data(), &fexErr_);

        // Step 5: feed the same IQ block to the panadapter analyzer.
        if (analyzerOpen_ && api.Spectrum0) {
            api.Spectrum0(1, kAnDisp, 0, 0, accum_.data());
        }

        // Peak |sample| across L+R as the audio-level proxy (Step 3d
        // is a measurement step — no playback).  20*log10(peak) dBFS.
        double peak = 0.0;
        const int outDoubles = 2 * outSize_;
        for (int i = 0; i < outDoubles; ++i) {
            const double a = std::fabs(outBuf_[static_cast<size_t>(i)]);
            if (a > peak) {
                peak = a;
            }
        }
        const double db = (peak > 0.0) ? 20.0 * std::log10(peak) : -200.0;
        audioDbFs_.store(db, std::memory_order_relaxed);

        // Step 3e: apply the operator volume/mute gain, convert to
        // int16 stereo, and push to the sound-card ring.  audioMtx_
        // guards audioRing_ against a main-thread device switch /
        // teardown.  gain = 0 when muted (SAFETY default at startup).
        {
            std::lock_guard<std::mutex> lk(audioMtx_);
            if (audioRing_) {
                const double gain =
                    muted_.load(std::memory_order_relaxed)
                        ? 0.0
                        : posToGain(volume_.load(std::memory_order_relaxed));
                for (int f = 0; f < outSize_; ++f) {
                    double l = std::clamp(
                        outBuf_[static_cast<size_t>(2 * f + 0)] * gain,
                        -1.0, 1.0);
                    double r = std::clamp(
                        outBuf_[static_cast<size_t>(2 * f + 1)] * gain,
                        -1.0, 1.0);
                    pcm16_[static_cast<size_t>(2 * f + 0)] =
                        static_cast<qint16>(l * 32767.0);
                    pcm16_[static_cast<size_t>(2 * f + 1)] =
                        static_cast<qint16>(r * 32767.0);
                }
                audioRing_->push(pcm16_.data(), outSize_);
            }
        }

        // Drop the consumed block; shift the small remainder down.
        accum_.erase(accum_.begin(),
                     accum_.begin() + static_cast<std::ptrdiff_t>(blockDoubles));
    }
}

} // namespace lyra::dsp
