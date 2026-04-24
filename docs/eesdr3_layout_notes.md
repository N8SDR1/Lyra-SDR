# ExpertSDR3 layout vocabulary — reference for Lyra UI

Source: ExpertSDR3 English User Manual v1.1.7, pages 57-102.
Screenshots rendered to `docs/eesdr3_refs/`.

We are not cloning EESDR3. We're lifting its **organizational pattern**
and the key widget idioms that make it feel like a modern SDR. Lyra
targets the same look-and-feel genre, not this specific layout.

## Overall shape

A **dense horizontal control strip** sits at the top of the window,
spanning most of the width in compact grouped panels. Below it, the
**panadapter + waterfall** fills the rest of the screen. A thin
**status bar** sits at the bottom.

The control strip is NOT a single flat row — it's 2-3 tall rows packed
with many small sub-panels, each with its own border/panel chrome.

## Top control strip, left → right

1. **Global panel** — Start/Stop, RX2, BS (bandscope), XVTR, ATT/preamp
2. **Volume/Monitor** — Vol slider + speaker mute; Monitor slider + phones toggle
3. **VFO area** — big 7-segment-style freq display for VFO A and VFO B,
   with SUB / SPLIT / RIT / XIT toggles nearby
4. **Mode + filter** — mode buttons (CW, LSB, USB, AM, NFM, DIGL, DIGU),
   filter-width preset buttons (narrow to wide)
5. **Band panel** — horizontal strip of band buttons (160m, 80m, 60m,
   40m, 30m, 20m, …) with per-band memory
6. **S-meter** — analog horizontal, dual scale: S-units below, dBm above;
   yellow needle = squelch threshold
7. **DSP panel** — NB (noise blanker), NR (noise reduction), ANF (auto
   notch), BIN (binaural), TUNE, AGC slider with level markers
8. **Secondary controls** — SQL, CTCSS, Line Out to MP3/soundcard,
   voice recorder, macros

## Panadapter panel

- Frequency scale across top with MHz labels
- **Colored band-indicator strip** at the very top (green/blue/orange
  bars showing current band and adjacent band edges)
- Spectrum trace with filled gradient beneath
- **VFO filter passband** shown as translucent colored box overlaying
  the spectrum trace (red-ish for VFO A, different color for VFO B)
- Zoom centers around mouse position; wheel to zoom
- RX and TX filter edges draggable

## Waterfall panel

- Intense saturated palette (dark blue → cyan → yellow → red/orange)
- Per-user-configurable color scheme in Visuals settings
- Zoom-center marker as vertical line
- Frequency labels at top; time scrolls downward

## Status bar

Small compact strip: connection-state icon, RX/TX filter info, PC sound
card routing, mic info, chain-link for filter lock, device-info popup.
All very small glyphs with cyan pill accents for active states.

## Visuals / theming

EESDR3 exposes individual color pickers for:
- Spectrum trace (fill + line)
- Waterfall palette
- Filter passband tint (VFO A, VFO B, RX, TX)
- Grid, background, labels
- Contrast / brightness sliders

**Implication:** themes should be data, not hardcoded colors. Every
custom-painted widget should read from a theme/palette object that a
settings dialog can edit.

## Key widget idioms to build

1. **GlassPanel** — rounded corners, subtle inner gradient,
   1-pixel accent rim. Every sub-panel uses this.
2. **GroupHeader** — small label strip above each sub-panel (e.g.,
   "DSP", "FILTER", "VFO").
3. **SegmentedButtonBar** — horizontal row of toggle buttons that
   look like one welded group (mode, filter preset, band).
4. **SevenSegDisplay** — big amber/cyan 7-seg frequency display with
   separator dots, clickable digits for editing.
5. **AnalogMeter** — needle-style horizontal meter with dual scale,
   peak-hold tail, optional secondary needle (squelch threshold).
6. **FilterShade** — translucent colored box drawn OVER the spectrum
   trace to show current RX (and TX) filter passband; edges draggable.
7. **BandStrip** — horizontal band-button row + matching colored tick
   at top of panadapter.
8. **StatusChip** — small pill with icon + short label, cyan when
   active. Used in status bar.

## Elements we can skip / defer

- Multiple RX windows (complex; defer)
- TCI/CAT integration panels (later milestone)
- Autostart / macro editor UI
- Full color-picker settings dialog (ship a dark default first, expose
  theme tokens later)
- E-Coder / MIDI-controller panels (hardware-specific)

## Proposed panel list for our refactor

Directly mappable from EESDR3 vocabulary; each is its own file and
widget class, all inheriting `GlassPanel`:

- `RadioStatusPanel` (start/stop + connection)
- `TuningPanel` (VFO A freq display + step)
- `ModeFilterPanel` (mode + RX/TX BW + lock + step)
- `BandPanel` (band button strip)
- `GainPanel` (LNA gain + volume + AGC)
- `DspPanel` (NB, NR, ANF, notch count, Q)
- `NotchPanel` (inside or alongside DspPanel)
- `SMeterPanel` (analog needle)
- `SpectrumPanel` + `WaterfallPanel` (already split as widgets)
- `StatusBarPanel` (bottom strip)

## Non-functional constraints

- All panels must be independently sized (no hardcoded widths tied to
  other panels).
- Every color/gradient/radius from `theme.py` — no inline hex codes.
- Widgets communicate with a single `Radio` controller object via Qt
  signals. No widget reaches into another's private state.
