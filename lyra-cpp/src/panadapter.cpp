// Lyra — panadapter spectrum display implementation (Step 5).
// See panadapter.h for scope.

#include "panadapter.h"

#include "wdsp_engine.h"

#include <QPainter>
#include <QPainterPath>

#include <algorithm>

namespace lyra::ui {

Panadapter::Panadapter(QQuickItem *parent) : QQuickPaintedItem(parent) {
    // Default render target (Image) — works under every RHI backend
    // incl. Vulkan (FramebufferObject is GL-only).
    timer_.setInterval(16);   // ~60 fps repaint (matches analyzer fps)
    connect(&timer_, &QTimer::timeout, this, &Panadapter::onTick);
    timer_.start();
}

QObject *Panadapter::engineObj() const {
    return reinterpret_cast<QObject *>(engine_);
}

void Panadapter::setEngineObj(QObject *o) {
    auto *e = qobject_cast<lyra::dsp::WdspEngine *>(o);
    if (e == engine_) {
        return;
    }
    engine_ = e;
    if (engine_) {
        pix_.assign(static_cast<size_t>(engine_->spectrumPixelCount()),
                    -200.0f);
    } else {
        pix_.clear();
    }
    emit engineChanged();
}

void Panadapter::setDbMin(double v) {
    if (v != dbMin_) { dbMin_ = v; emit rangeChanged(); update(); }
}

void Panadapter::setDbMax(double v) {
    if (v != dbMax_) { dbMax_ = v; emit rangeChanged(); update(); }
}

void Panadapter::onTick() {
    if (!engine_ || pix_.empty() || !isVisible()) {
        return;
    }
    engine_->copySpectrum(pix_.data(), static_cast<int>(pix_.size()));
    update();   // schedule a repaint
}

void Panadapter::paint(QPainter *p) {
    const qreal w = width();
    const qreal h = height();

    // Step 5a: plain background + plain trace (prove the analyzer feed).
    // Glass + smooth fluid curve + gradient fill land in Step 5b.
    p->fillRect(QRectF(0, 0, w, h), QColor(0x0a, 0x0e, 0x12));
    const int n = static_cast<int>(pix_.size());
    if (n < 2 || w < 2.0 || h < 2.0) {
        return;
    }

    p->setRenderHint(QPainter::Antialiasing, true);

    const double range = (dbMax_ - dbMin_);
    const double invRange = (range != 0.0) ? 1.0 / range : 1.0;
    auto yFor = [&](float db) -> qreal {
        double t = (static_cast<double>(db) - dbMin_) * invRange;
        t = std::clamp(t, 0.0, 1.0);
        return h - t * h;   // higher dB -> higher on screen
    };

    QPainterPath path;
    for (int i = 0; i < n; ++i) {
        const qreal x = static_cast<qreal>(i) / (n - 1) * w;
        const qreal y = yFor(pix_[static_cast<size_t>(i)]);
        if (i == 0) {
            path.moveTo(x, y);
        } else {
            path.lineTo(x, y);
        }
    }
    p->setPen(QPen(QColor(0x7f, 0xdf, 0xff), 1.2));
    p->drawPath(path);
}

} // namespace lyra::ui
