# S-Meter

*(Tip: [click here](panel:meters) to flash the Meters panel if you
can't find it among your docked windows.)*

## Styles

Two styles, switchable via right-click on the meter:

### Analog needle
Classic Kenwood/Yaesu aesthetic — shallow-arc dial with concentric
scales, cream face with lit-amber markings on black. Single white
needle tracks the signal level. Amber digital frequency readout.

### LED bar-graph
Modern Icom/Yaesu aesthetic — segmented colored bars. Stacked rows
for different meter types (S-meter during RX; PWR, SWR, ALC, MIC
during TX when the TX path ships).

Both styles share the same underlying data feed.

## Calibration

S-meter follows the standard **S1 = −121 dBm, 6 dB per S-unit**
convention above the preamp stage. HL2 gain setting is compensated
so S-readings are consistent across different RF gain values.

A **user calibration** offset will be added to Settings (backlog item)
for fine-tuning against a known reference signal.

## Moving / resizing the meter

The S-Meter panel is a **dockable widget**. Drag the title bar to
undock it into a floating window. Resize by dragging the edges. Snap
back by dragging the title bar into any dock area.

**View → S-Meter** toggles its visibility.

## TX-side meters (future)

When TX ships, the meter will auto-switch to show:

- **PWR** — forward power, calibrated watts
- **SWR** — reflected/forward ratio
- **ALC** — automatic level control headroom
- **MIC** — mic input level (for mic gain setup)
- **PROC / MONI / CH1** — indicators for speech processor / side-tone

All sharing the same multi-meter data feed and user-selectable style.
