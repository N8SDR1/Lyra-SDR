# Spectrum & Waterfall

## Reading the display

- **Spectrum** (top) — live FFT magnitude in dBFS. The bright trace
  is the current frame; the dim trace behind is a peak-hold.
- **Waterfall** (bottom) — scrolling time-history. Each row is one
  FFT. Newer rows at the top; older at the bottom.
- **Center marker** — dashed orange vertical line at the RX center
  frequency.
- **RX passband overlay** — translucent cyan rectangle spanning the
  current mode's filter window (USB = carrier to +BW, LSB = −BW to
  carrier, CW = narrow box around the CW pitch, AM/FM = ±BW/2). Any
  signal inside the rect is reaching the demod; signals outside are
  blocked by the filter. Updates live as you change mode / RX BW.
- **Notch wells** — red V-shaped overlays descending from the top of
  the panadapter for every active notch. The well contour follows
  the biquad magnitude response so you see both **width** (horizontal
  extent) and **depth** (how far the well drops, labeled up to
  −40 dB). See the [Notch Filters](notches.md) topic.
- **Band-plan overlay** — optional colored strip at the top of the
  spectrum showing regional sub-band allocations (CW / DIG / SSB / FM)
  with landmark triangles at FT8/FT4/WSPR/PSK frequencies. Click a
  landmark to tune there + switch to its suggested mode.
- **TCI spots** — small colored boxes with callsign text, stacked
  above the spectrum. See below.

## Noise-floor reference line

A muted dashed horizontal line across the panadapter marks the current
noise floor, with a small `NF −NN dBFS` label at the right edge.
Computed as the **20th percentile** of the current FFT bins,
rolling-averaged over ~1 second, further EMA-smoothed so the line
doesn't jitter.

Use it to gauge signal-to-noise at a glance: any signal peak more
than ~6 dB above the NF line is solidly above the floor and will
demodulate cleanly; peaks within 3 dB of the line are marginal.

**Toggle**: Settings → Visuals → Signal range → "Show noise-floor
reference line". Default on. Persists across launches.

**Color**: Settings → Visuals → Colors → **Noise-floor** field. The
label text itself is painted in the currently-chosen color + bold so
you can see at a glance what it's set to. Click the label to aim it,
then click any preset chip (or "Custom color…") to change it.

The NF estimate also feeds AGC's auto-threshold feature (right-click
AGC cluster → Auto-calibrate), so having them both on gives a
self-consistent view: the AGC target sits ~18 dB above the NF line
by default.

## Peak markers

Optional peak-hold overlay drawn **only inside the RX filter passband**.
Shows the highest dB level each bin has reached in the decay window —
useful for spotting weak signals that come and go too fast for the
eye to catch, or confirming a recent burst peaked above a threshold.

Unlike whole-spectrum "blobs" in some reference SDR clients, Lyra's
peak markers are scoped to the passband so the feature doesn't clutter
the whole spectrum with irrelevant peaks.

- **Toggle**: Settings → Visuals → Signal range → "Show peak markers"
- **Style**: three render options — **Line** (solid peak trace),
  **Dots** (discrete per-bin markers), **Triangles** (upward
  triangles at peaks). Live-switch from the Visuals tab.
- **Show dB readout**: optional numeric dB value drawn next to the
  three highest in-passband peaks.
- **Decay rate slider**: 1 – 120 dB/second. Lower = peaks linger
  longer (good for watching band openings, spotting DX). Higher =
  peaks track the signal closely (live-action feel).
- **Color**: Settings → Visuals → Colors → **Peak markers** field.
  Separate from the main trace color so the two don't blend.
- Default: off, Dots style, 10 dB/s when enabled.

## ADC peak indicator (toolbar)

The `ADC  -NN.N dBFS` readout on the main toolbar is the live peak
envelope of the incoming IQ stream — the single best diagnostic for
RF-chain health. Color-coded:

| Reading         | Color  | What it means                            |
|-----------------|--------|------------------------------------------|
| > −3 dBFS       | Red    | Clipping — drop LNA immediately          |
| −3 to −10 dBFS  | Orange | Hot — IMD products likely                |
| −10 to −30 dBFS | Green  | Sweet spot                               |
| −30 to −50 dBFS | Cyan   | Acceptable / weak-signal friendly        |
| < −50 dBFS      | Gray   | Low — raise LNA or check antenna/feedline |

Use this to calibrate LNA gain for your RF environment instead of
guessing. If the reading is in the green and weak signals still
don't come through, the issue is likely antenna/coax, not the HL2.

## TCI spot boxes

Spots pushed by logging / cluster software via TCI appear as colored
boxes above the trace, with a tick line pointing down to the exact
frequency.

- Up to **4 rows** of spots with collision-aware packing — newest
  spots claim the top row.
- **Age-fade** — oldest spots fade toward 30 % alpha as they approach
  their lifetime.
- **Click** a spot box (or its tick line) to tune and switch to that
  mode.
- See **TCI Server** topic for all spot settings.

## Click-to-tune

**Left-click** anywhere on the spectrum or waterfall to tune that
frequency. Click-to-tune honors landmark triangles on the band-plan
overlay — clicking a triangle tunes AND switches to the landmark's
suggested mode (FT8 → DIGU, WSPR → DIGU, PSK → DIGU, etc.).

## Right-click

**When Notch Filter is ON** — opens the notch context menu (Add /
Remove nearest / Clear all / Default-Q submenu / Disable). See the
[Notch Filters](notches.md) topic.

**When Notch Filter is OFF** — opens a minimal menu with a single
"Enable Notch Filter" item. This gating keeps right-click free for
future spectrum features (drag-to-tune, spot menus, etc.) whenever
you're not actively working notches.

## Mouse wheel

- **Over empty spectrum** — zooms bandwidth. Up = zoom in, down =
  zoom out. Steps through the preset zoom levels (1× / 2× / 4× / 8×
  / 16×).
- **Over a notch well** — adjusts that notch's Q (width). Up = higher
  Q (narrower), down = lower Q (wider).

## Draggable overlays

- **Passband edges** (dashed cyan lines on either side of the
  passband rect) — grab and drag horizontally to adjust the current
  mode's RX BW on the fly. The Settings → Mode + Filter BW combo
  updates live.
- **Notch wells** — left-drag vertically over a notch to fine-tune
  its Q. Drag up = higher Q (narrower); down = lower Q (wider).
- **dB-scale Y-axis (rightmost 50 px strip)** — drag vertically in
  the right-edge zone to rescale the spectrum:
  - Top third → adjusts `max_db` (pulls the top of the scale)
  - Middle third → pans both edges together
  - Bottom third → adjusts `min_db` (pulls the floor)
  Cursor changes to a vertical-resize arrow when you're in the zone.
  Saves back to Settings → Visuals → Signal range automatically.

## dB range — Settings → Visuals

Four sliders (spectrum min/max + waterfall min/max, each −150 … 0
dBFS) live in **Settings → Visuals → Signal range**. Defaults:

- **Spectrum:** −110 to −20 dBFS
- **Waterfall:** −110 to −30 dBFS

Moving a slider updates the display in real time. Span is clamped to
≥ 3 dB so you can't accidentally collapse the trace to a flat line
by crossing the min/max over. A **Reset to defaults** button restores
the factory values.

For ad-hoc adjustment during operating, the Y-axis drag on the right
edge of the spectrum (above) is usually faster than opening Settings.

If signals are slamming the top of the scale, either raise `max` a
few dB to see detail above the peaks, or reduce RF gain via the LNA
slider on the [DSP & AUDIO panel](panel:dsp).

## Colors

Every user-pickable color (spectrum trace, noise-floor line, peak
markers, and each band-plan segment CW/DIG/SSB/FM) has its own entry
in **Settings → Visuals → Colors**. Layout:

- Each field is a **clickable colored label** — the label's own text
  is painted in that field's current color and bolded, so you can
  read the whole palette at a glance. Click a label to aim it.
- An inline 18-chip **preset palette** sits below the labels. Click
  any preset to apply it to the aimed field. Two clicks total per
  change (aim → pick).
- **Custom color…** button opens a full `QColorDialog` for colors
  not in the presets.
- **Right-click any label** or the **Reset aimed** button returns
  that one field to its factory default.
- **Reset all** returns every color back to factory defaults in one
  go.

## Waterfall palette

**Settings → Visuals → Waterfall palette** — eight built-ins, live
switch. Changes apply from the next FFT row onward; rows already on
screen keep their existing colors until they scroll off.

| Palette       | Character                                          |
|---------------|----------------------------------------------------|
| **Classic**   | Icy blue → cyan → yellow → red. Lyra's default — the reference-client look. |
| **Inferno**   | Dark purple → orange → yellow. High contrast, scientific-grade. |
| **Viridis**   | Deep purple → teal → yellow-green. Color-blind friendly, perceptually uniform. |
| **Plasma**    | Deep blue → magenta → orange → yellow. Warm, band-opening vibe. |
| **Rainbow**   | Full rainbow. Old-school SDR look; easy to spot peaks. |
| **Ocean**     | Black → navy → teal → white. Cool, easy on the eyes. |
| **Night**     | Black → deep red → orange. Preserves dark-adapted vision for late DX. |
| **Grayscale** | Black → white. Useful for screenshots / printing. |

## Graphics backend — Software / OpenGL

**Settings → Visuals → Graphics backend** picks whether painted
widgets rasterize on the CPU (Software, default) or the GPU
(OpenGL). Same `QPainter` code either way — OpenGL just moves the
rasterization off the main Python thread, which keeps audio smooth
during window resize / fullscreen toggle.

- **Software (QPainter)** — always works, default. Every Windows GPU
  from the last 20 years runs it fine.
- **OpenGL** — recommended on any modern machine (really anything
  Intel HD 4000 / 2012 and newer). Needs a restart after selection.
- **Vulkan** — listed but greyed out; `QVulkanWindow` needs a full
  shader rewrite, not on the near-term roadmap.

If OpenGL fails to initialize for any reason (bad driver, remote
session, headless CI), Lyra silently falls back to Software. The
Visuals tab shows which backend is actually live alongside the one
you selected.

## Update rates and zoom

Three independent controls in **Settings → Visuals → Update rates
and zoom**. Changes are live — no restart needed. Front-panel **VIEW**
strip (Zoom / Spec / WF sliders) mirrors these settings both ways —
adjust in either place, the other updates.

### Panadapter zoom

Crops the FFT to a centered subset of bins so the panadapter
magnifies around your RX frequency. Levels: **1× / 2× / 4× / 8× /
16×**. No impact on DSP, demod, or recorded IQ — it's purely a
display change.

Two ways to switch:
- **Mouse wheel** on empty spectrum (not over a notch well) — each
  tick steps one level. Up = zoom in, down = zoom out.
- **Settings → Visuals → Panadapter zoom** combo, or the front-panel
  **VIEW** Zoom slider.

At 16× zoom on a 48 kHz sample rate, you're seeing 3 kHz of span —
about a single SSB channel wide. Great for zeroing CW or watching
FT8 lanes.

### Spectrum rate

FFT / repaint rate, **5 – 120 fps**, default 30. Lower = less CPU
and GPU load (useful on older laptops or when running alongside heavy
logging software); higher = smoother trace during fast tuning.

### Waterfall rate

Independent of spectrum FPS. The waterfall pushes **1 row every N
FFT ticks** with a multiplier of **1× – 10×** at the fast end. Slider
right = fast scrolling / high-rate roll, slider left = slow crawl
with more time history visible on-screen at once.

Example: spectrum at 30 fps, waterfall divider 10 → waterfall
scrolls at 3 rows/sec (a full screen ≈ 170 s of history). At max
multiplier (10×) the waterfall can push up to ~30 × 10 = 300 rows/sec
which reads as a near-instant live scroll.

## Performance notes

- **Resize / fullscreen stutter** — largely resolved by switching the
  graphics backend to OpenGL (above). On Software backend, resizing
  the window can still pause audio briefly because the demod runs on
  the main thread.
- If the spectrum hiccups, check CPU usage. Closing the waterfall
  panel temporarily (**View → Waterfall**) cuts FFT work roughly in
  half.
