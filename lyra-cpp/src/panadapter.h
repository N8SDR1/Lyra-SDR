// Lyra — panadapter spectrum display (Step 5).
//
// A QQuickPaintedItem that pulls the WDSP analyzer's display-width dB
// array from WdspEngine (~30 fps) and paints it.  Step 5a: a plain
// trace to prove the analyzer feed works on real signal.  Step 5b
// restyles paint() into the operator's glass background + smooth fluid
// curve + gradient fill (and a matching glassy waterfall later).
//
// Rendered with QPainter (RHI-backed under Vulkan) — simplest path to
// smooth AA curves + gradient fills + glass; can drop to a custom
// scene-graph node later if performance ever demands it.
//
// The engine is passed in from QML as a QObject* (the WdspEngine
// context property) and qobject_cast internally — avoids needing the
// WdspEngine* metatype registered with QML.

#pragma once

#include <QQuickPaintedItem>
#include <QTimer>

#include <vector>

namespace lyra::dsp { class WdspEngine; }

namespace lyra::ui {

class Panadapter : public QQuickPaintedItem {
    Q_OBJECT
    Q_PROPERTY(QObject *engine READ engineObj WRITE setEngineObj
               NOTIFY engineChanged)
    // dB display window (vertical scale).  Operator-tunable later.
    Q_PROPERTY(double dbMin READ dbMin WRITE setDbMin NOTIFY rangeChanged)
    Q_PROPERTY(double dbMax READ dbMax WRITE setDbMax NOTIFY rangeChanged)

public:
    explicit Panadapter(QQuickItem *parent = nullptr);

    QObject *engineObj() const;
    void     setEngineObj(QObject *o);

    double dbMin() const { return dbMin_; }
    void   setDbMin(double v);
    double dbMax() const { return dbMax_; }
    void   setDbMax(double v);

    void paint(QPainter *p) override;

signals:
    void engineChanged();
    void rangeChanged();

private slots:
    void onTick();

private:
    lyra::dsp::WdspEngine *engine_ = nullptr;
    QTimer                 timer_;
    std::vector<float>     pix_;       // dB pixels from the analyzer
    double                 dbMin_ = -130.0;
    double                 dbMax_ = -20.0;
};

} // namespace lyra::ui
