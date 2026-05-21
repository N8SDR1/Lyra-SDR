// Lyra — WDSP RX channel engine implementation (Step 3c-ii).
// See wdsp_engine.h for the locked scope + the §14.2 gotcha list.

#include "wdsp_engine.h"

#include <QDebug>

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

} // namespace

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

    // Start the channel (state=running, dmode=0).
    api.SetChannelState(channel_, 1, 0);
    running_ = true;
    emit runningChanged();

    emitLog(QStringLiteral(
        "[wdsp] channel 0 opened (192k IQ -> 48k audio, USB "
        "200-3000 Hz, AGC MED, binaural mono); out_size=%1 frames")
        .arg(outSize_));
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
    emitLog(QStringLiteral("[wdsp] channel 0 closed"));
}

} // namespace lyra::dsp
