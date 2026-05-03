"""Central theme / style constants.

Every color, gradient, pen, font, and radius in the app reads from here.
Changing the look of the app later = one file edit. The main stylesheet
is assembled from these tokens so Qt widgets and custom-painted widgets
stay visually aligned.
"""
from __future__ import annotations

from PySide6.QtGui import QColor


# Palette ported from the SDRLogger+ web app (Y:\Claude local\hamlog),
# which the user wants as the starting look for Lyra.
# Cool-CRT dark scheme: blue-black surfaces, electric cyan primary,
# neon-green secondary, amber-orange warn.

# ── Surfaces ────────────────────────────────────────────────────────────
BG_APP       = QColor(0x0a, 0x0d, 0x12)  # deep blue-black
BG_PANEL     = QColor(0x11, 0x16, 0x20)  # panel surface
BG_RECESS    = QColor(0x10, 0x14, 0x1c)  # inset grooves, slightly darker
BG_CTRL      = QColor(0x16, 0x1c, 0x28)  # buttons, inputs (cards)

# ── Accents ─────────────────────────────────────────────────────────────
ACCENT       = QColor(0x00, 0xe5, 0xff)  # electric cyan — primary
ACCENT_DIM   = QColor(0x00, 0xa8, 0xc8)  # darker cyan for gradients
ACCENT2      = QColor(0x39, 0xff, 0x14)  # neon green — secondary / active
ACCENT_WARM  = QColor(0xff, 0xd7, 0x00)  # gold — alert
ACCENT_HOT   = QColor(0xff, 0x6b, 0x35)  # amber-orange — warn
ACCENT_NOTCH = QColor(0xff, 0x44, 0x44)  # notch / error red
ACCENT_PEAK  = QColor(0xff, 0xff, 0xff)  # peak-hold highlight

# ── Text ────────────────────────────────────────────────────────────────
TEXT_PRIMARY = QColor(0xcd, 0xd9, 0xe5)  # cool off-white
TEXT_MUTED   = QColor(0x8a, 0x9a, 0xac)  # dusty blue-gray
TEXT_FAINT   = QColor(0x5a, 0x70, 0x80)  # labels, axis tick marks

# ── Borders / grids ─────────────────────────────────────────────────────
BORDER       = QColor(0x1e, 0x2a, 0x3a)  # subtle panel rim
BORDER_HI    = QColor(0x00, 0xe5, 0xff)  # focus / hover rim
GRID         = QColor(0x1e, 0x2a, 0x3a)  # spectrum grid

# ── Status chip colors (SDRLogger+ parity) ──────────────────────────────
CHIP_OK      = QColor(0x00, 0xc8, 0x51)  # green
CHIP_WARN    = QColor(0xff, 0xd7, 0x00)  # gold
CHIP_HOT     = QColor(0xff, 0x6b, 0x35)  # orange
CHIP_ERR     = QColor(0xff, 0x44, 0x44)  # red

# ── Radii / thicknesses ─────────────────────────────────────────────────
PANEL_RADIUS = 6
CTRL_RADIUS  = 3
BTN_RADIUS   = 4
PEN_TRACE    = 1.2
PEN_GRID     = 1.0

# ── Fonts (Google fonts — fall back cleanly if not installed) ───────────
# Share Tech Mono: tech-terminal monospace for numeric readouts & badges
# Rajdhani: bold geometric sans for headings / panel titles
# Exo 2: modern sans for body text / labels
FONT_BODY    = "Exo 2, Segoe UI, sans-serif"
FONT_HEAD    = "Rajdhani, Segoe UI Semibold, sans-serif"
FONT_MONO    = "Share Tech Mono, Consolas, monospace"
# Global default font size — bumped 10 -> 11pt v0.0.6.x for
# better readability on dense Settings tabs (Noise, Visuals).
# Bigger panels stay tight via explicit per-widget sizing
# overrides where needed.
FONT_SIZE    = 11

# Convenience single-family picks for QFont(...) calls
FONT_FAMILY  = "Exo 2"
FONT_MONO_FAMILY = "Share Tech Mono"
FONT_HEAD_FAMILY = "Rajdhani"


def qss_color(c: QColor, alpha: int | None = None) -> str:
    """Format a QColor for inclusion in a Qt stylesheet."""
    if alpha is None:
        return f"rgb({c.red()}, {c.green()}, {c.blue()})"
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def build_stylesheet() -> str:
    """Full app stylesheet assembled from the theme tokens above."""
    return f"""
QMainWindow, QWidget {{
    background: {qss_color(BG_APP)};
    color: {qss_color(TEXT_PRIMARY)};
}}
QLabel {{ color: {qss_color(TEXT_MUTED)}; }}

/* Tooltips — without this rule Qt falls back to OS defaults, which
   on a dark UI render as white-on-white (Win11 light tooltip on a
   dark widget) and become unreadable. The notch counter on the
   DSP+Audio panel was the worst offender — its multiline tooltip
   describing the right-click menu was effectively invisible.

   Font size is bumped to 11 pt — the OS default of 8-9 pt is
   readable on small text but too cramped for our multi-line
   gesture-cheat-sheet tooltips. Padding is also more generous so
   the text doesn't crowd the border. */
QToolTip {{
    background: {qss_color(BG_PANEL)};
    color: {qss_color(TEXT_PRIMARY)};
    border: 1px solid {qss_color(BORDER_HI)};
    border-radius: 4px;
    padding: 8px 10px;
    font-size: 13pt;
    /* v0.0.9 bump 11pt -> 13pt for readability.  Matched in
       app.py via QToolTip.setFont() since QSS font-size on
       QToolTip is honored unreliably on Windows native plugin.
       opacity is set as a separate Qt property attr in code if
       we ever want translucent tooltips; default is fully
       opaque. */
}}
QLineEdit, QComboBox {{
    background: {qss_color(BG_CTRL)};
    color: {qss_color(TEXT_PRIMARY)};
    border: 1px solid {qss_color(BORDER)};
    border-radius: {CTRL_RADIUS}px;
    padding: 4px 6px;
    selection-background-color: {qss_color(ACCENT, 80)};
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {qss_color(ACCENT)};
}}
/* QDoubleSpinBox styling intentionally lives in the
   QAbstractSpinBox block below — that block covers BOTH
   QSpinBox and QDoubleSpinBox via the common base class. The
   earlier QLineEdit rule was reserving only 6 px of right-side
   padding which clobbered the up/down spin buttons (the up
   button's hit region landed under the border + clipped by the
   too-tight padding). The QAbstractSpinBox padding reserves 20 px
   for the buttons so up/down both work cleanly. */
QPushButton {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 {qss_color(BG_CTRL)}, stop:1 {qss_color(BG_RECESS)});
    color: {qss_color(TEXT_PRIMARY)};
    border: 1px solid {qss_color(BORDER)};
    border-radius: {BTN_RADIUS}px;
    padding: 5px 12px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}
QPushButton:hover {{
    border-color: {qss_color(ACCENT)};
    color: {qss_color(ACCENT)};
}}
QPushButton:checked {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 {qss_color(ACCENT_DIM)}, stop:1 {qss_color(BG_CTRL)});
    border-color: {qss_color(ACCENT)};
    color: {qss_color(BG_APP)};
}}
QPushButton:pressed {{ background: {qss_color(BG_RECESS)}; }}
QPushButton:disabled {{
    background: {qss_color(BG_RECESS)};
    color: {qss_color(TEXT_FAINT)};
    border-color: {qss_color(BORDER)};
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {qss_color(BG_RECESS)};
    border-radius: 2px;
    border: 1px solid {qss_color(BORDER)};
}}
QSlider::handle:horizontal {{
    background: {qss_color(ACCENT)};
    width: 12px; margin: -6px 0;
    border-radius: 2px;
    border: 1px solid {qss_color(BG_APP)};
}}
QSlider::handle:horizontal:hover {{ background: {qss_color(ACCENT2)}; }}

/* Per-purpose colored slider variants. Set the objectName on the
   slider to one of the names below to get its color. Standard
   ham-radio color language: green volume, amber gain, blue AGC,
   red drive. */
QSlider#vol_slider::handle:horizontal {{
    background: #39ff14;
    border: 1px solid #0a3010;
}}
QSlider#vol_slider::handle:horizontal:hover {{ background: #6fff60; }}

QSlider#gain_slider::handle:horizontal {{
    background: #ffab47;
    border: 1px solid #4a2a08;
}}
QSlider#gain_slider::handle:horizontal:hover {{ background: #ffd07a; }}

QSlider#q_slider::handle:horizontal {{
    background: #ff6b35;
    border: 1px solid #4a1a08;
}}
QSlider#q_slider::handle:horizontal:hover {{ background: #ff9b75; }}

QSlider#agc_slider::handle:horizontal {{
    background: #50d0ff;
    border: 1px solid #103040;
}}
QSlider#agc_slider::handle:horizontal:hover {{ background: #80ddff; }}

QSlider#drive_slider::handle:horizontal {{
    background: #ff4444;
    border: 1px solid #4a0808;
}}
QSlider#drive_slider::handle:horizontal:hover {{ background: #ff8080; }}

/* DSP button row: square compact toggles. Off = dim cyan-tinged
   border; On = bright orange fill. */
QPushButton#dsp_btn {{
    min-width: 38px;
    max-width: 56px;
    padding: 4px 6px;
    font-weight: 700;
    letter-spacing: 1px;
    color: {qss_color(TEXT_MUTED)};
    background: {qss_color(BG_RECESS)};
    border: 1px solid {qss_color(BORDER)};
    border-radius: 3px;
}}
QPushButton#dsp_btn:hover {{
    color: {qss_color(TEXT_PRIMARY)};
    border-color: {qss_color(ACCENT)};
}}
QPushButton#dsp_btn:checked {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #ff8c1a, stop:1 #cc5500);
    color: #1a0f00;
    border-color: #ff6600;
    font-weight: 800;
}}
QPushButton#dsp_btn:checked:hover {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #ffaa44, stop:1 #ee7711);
    border-color: #ffaa44;
}}
QFrame#panel {{
    background: {qss_color(BG_PANEL)};
    border: 1px solid {qss_color(BORDER)};
    border-radius: {PANEL_RADIUS}px;
}}
QStatusBar {{
    background: {qss_color(BG_RECESS)};
    color: {qss_color(TEXT_MUTED)};
    border-top: 1px solid {qss_color(BORDER)};
}}

/* ── Dockable panel styling ──────────────────────────────────── */
/* Keep this minimal — QDockWidget title bars lose interactivity if
   you override the close/float icons to url(none) or add invalid
   properties like text-align. Style ONLY the colors here; let Qt
   handle layout and hit-testing natively. */
QDockWidget {{
    color: {qss_color(TEXT_PRIMARY)};
    font-weight: 600;
    border: 1px solid {qss_color(BORDER)};
}}
QDockWidget::title {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 {qss_color(BG_PANEL)}, stop:0.5 {qss_color(BG_CTRL)},
        stop:1 {qss_color(BG_RECESS)});
    color: {qss_color(ACCENT)};
    padding-left: 8px;
    padding-top: 6px;
    padding-bottom: 6px;
    border-bottom: 1px solid {qss_color(ACCENT, 100)};
}}
QDockWidget::close-button, QDockWidget::float-button {{
    subcontrol-position: right center;
    padding: 1px;
    margin-right: 2px;
    border: 1px solid transparent;
    border-radius: 3px;
    background: transparent;
}}
QDockWidget::close-button:hover, QDockWidget::float-button:hover {{
    background: {qss_color(ACCENT, 60)};
    border: 1px solid {qss_color(ACCENT)};
}}

QTabWidget::pane {{
    background: {qss_color(BG_PANEL)};
    border: 1px solid {qss_color(BORDER)};
    border-radius: 4px;
    top: -1px;
}}
QTabBar::tab {{
    background: {qss_color(BG_RECESS)};
    color: {qss_color(TEXT_MUTED)};
    padding: 6px 14px;
    margin-right: 2px;
    border: 1px solid {qss_color(BORDER)};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    min-width: 70px;
}}
QTabBar::tab:hover {{
    color: {qss_color(TEXT_PRIMARY)};
    background: {qss_color(BG_CTRL)};
    border-color: {qss_color(ACCENT)};
}}
QTabBar::tab:selected {{
    background: {qss_color(BG_PANEL)};
    color: {qss_color(ACCENT)};
    border-color: {qss_color(ACCENT)};
    border-bottom: 1px solid {qss_color(BG_PANEL)};
    font-weight: 700;
}}

QGroupBox {{
    color: {qss_color(ACCENT)};
    font-weight: 700;
    border: 1px solid {qss_color(BORDER)};
    border-radius: 4px;
    margin-top: 14px;
    padding-top: 8px;
    padding-left: 6px;
    padding-right: 6px;
    padding-bottom: 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    background: {qss_color(BG_APP)};
    color: {qss_color(ACCENT)};
}}

QCheckBox {{
    color: {qss_color(TEXT_PRIMARY)};
    spacing: 7px;
}}
/* Indicator: bumped 14 -> 16 px for better visual weight, and the
   unchecked-state border uses TEXT_MUTED (a dusty blue-gray) rather
   than BORDER (very dark) so empty checkboxes are clearly visible
   against the dark BG_RECESS recess.  Operator feedback v0.0.6:
   "Tick boxes blend into the background." */
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    background: {qss_color(BG_RECESS)};
    border: 1px solid {qss_color(TEXT_MUTED)};
    border-radius: 3px;
}}
QCheckBox::indicator:hover {{
    border-color: {qss_color(ACCENT)};
    background: {qss_color(BG_PANEL)};
}}
QCheckBox::indicator:checked {{
    background: {qss_color(ACCENT)};
    border-color: {qss_color(ACCENT)};
    /* Built-in checkmark is rendered by Qt's native style — on
       cyan (ACCENT) bg this shows as a darker pip which is the
       contrast we want.  No explicit image: keeps the styling
       scalable across DPI without bundling SVG assets. */
}}
QCheckBox::indicator:checked:hover {{
    /* Slightly brighter ACCENT on hover-checked so the
       indicator visibly responds even when already on. */
    background: {qss_color(ACCENT.lighter(115))};
    border-color: {qss_color(ACCENT.lighter(115))};
}}
QCheckBox:disabled {{
    color: {qss_color(TEXT_MUTED)};
}}
QCheckBox::indicator:disabled {{
    border-color: {qss_color(BORDER)};
    background: {qss_color(BG_RECESS)};
}}

/* QRadioButton — round cousin of QCheckBox. Without this block Qt's
   default rendering can produce an invisible indicator on dark
   palettes (especially on Windows), which makes users think the
   radio isn't responding to clicks when it actually is. */
QRadioButton {{
    color: {qss_color(TEXT_PRIMARY)};
    spacing: 6px;
    padding: 3px 0;
}}
QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    background: {qss_color(BG_RECESS)};
    /* Same brightness bump as QCheckBox — TEXT_MUTED border so
       unchecked indicators are visible against the dark recess. */
    border: 1px solid {qss_color(TEXT_MUTED)};
    border-radius: 9px;
}}
QRadioButton::indicator:hover {{
    border-color: {qss_color(ACCENT)};
    background: {qss_color(BG_PANEL)};
}}
QRadioButton::indicator:checked {{
    /* Inner-dot look: tint the whole disc accent, with a BG-colored
       ring between dot and outer border so it reads as "filled dot". */
    background: qradialgradient(
        cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0   {qss_color(ACCENT)},
        stop:0.45 {qss_color(ACCENT)},
        stop:0.55 {qss_color(BG_RECESS)},
        stop:1    {qss_color(BG_RECESS)});
    border-color: {qss_color(ACCENT)};
}}
QRadioButton::indicator:disabled {{
    background: {qss_color(BG_RECESS)};
    border-color: {qss_color(BORDER)};
}}
QRadioButton:disabled {{
    color: {qss_color(TEXT_FAINT)};
}}

QAbstractSpinBox {{
    background: {qss_color(BG_CTRL)};
    color: {qss_color(TEXT_PRIMARY)};
    border: 1px solid {qss_color(BORDER)};
    border-radius: 3px;
    /* Right-side padding reserves room for both spin buttons. Without
       this reserved gutter, native buttons get clipped and the up-
       button hit region can land under the border. */
    padding: 3px 20px 3px 4px;
}}
QAbstractSpinBox:focus {{ border-color: {qss_color(ACCENT)}; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border;
    width: 16px;
    background: {qss_color(BG_CTRL)};
    border-left: 1px solid {qss_color(BORDER)};
}}
QAbstractSpinBox::up-button {{
    subcontrol-position: top right;
    border-bottom: 1px solid {qss_color(BORDER)};
    border-top-right-radius: 2px;
}}
QAbstractSpinBox::down-button {{
    subcontrol-position: bottom right;
    border-bottom-right-radius: 2px;
}}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background: {qss_color(ACCENT_DIM)};
}}
QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {{
    background: {qss_color(BG_RECESS)};
}}
/* CSS-triangle arrows — no image assets needed. Width=0 + two
   transparent borders + one colored border = triangle. */
QAbstractSpinBox::up-arrow {{
    width: 0px; height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {qss_color(TEXT_PRIMARY)};
}}
QAbstractSpinBox::up-arrow:hover {{
    border-bottom-color: {qss_color(ACCENT)};
}}
QAbstractSpinBox::down-arrow {{
    width: 0px; height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {qss_color(TEXT_PRIMARY)};
}}
QAbstractSpinBox::down-arrow:hover {{
    border-top-color: {qss_color(ACCENT)};
}}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{
    border-bottom-color: {qss_color(TEXT_FAINT)};
}}
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{
    border-top-color: {qss_color(TEXT_FAINT)};
}}

QSplitter::handle {{
    background: {qss_color(BG_RECESS)};
    border: 1px solid {qss_color(BORDER)};
}}
QSplitter::handle:hover {{
    background: {qss_color(ACCENT, 120)};
    border-color: {qss_color(ACCENT)};
}}
QSplitter::handle:vertical {{
    height: 5px;
    margin: 0px 4px;
}}
QSplitter::handle:horizontal {{
    width: 5px;
    margin: 4px 0px;
}}

QMenuBar {{
    background: {qss_color(BG_APP)};
    color: {qss_color(TEXT_PRIMARY)};
    border-bottom: 1px solid {qss_color(BORDER)};
}}
QMenuBar::item {{
    background: transparent;
    padding: 4px 10px;
}}
QMenuBar::item:selected {{
    background: {qss_color(ACCENT, 50)};
    color: {qss_color(ACCENT)};
}}
QMenu {{
    background: {qss_color(BG_PANEL)};
    color: {qss_color(TEXT_PRIMARY)};
    border: 1px solid {qss_color(BORDER)};
}}
QMenu::item:selected {{
    background: {qss_color(ACCENT, 50)};
    color: {qss_color(ACCENT)};
}}
"""
