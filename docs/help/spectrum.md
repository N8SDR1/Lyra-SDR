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
- **Notch rectangles** — filled red rectangles spanning each notch's
  −3 dB bandwidth, with a thin red center line for precise targeting.
  Active notches are saturated red; inactive (bypassed) notches are
  desaturated grey so you can A/B without losing placement. Width is
  in Hz, labeled next to the rectangle when there's room. See the
  [Notch Filters](notches.md) topic.
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
frequency.  Click-to-tune honors landmark triangles on the band-plan
overlay — clicking a triangle tunes AND switches to the landmark's
suggested mode (FT8 → DIGU, WSPR → DIGU, PSK → DIGU, etc.).

### Shift+click — snap to nearest peak (v0.0.8)

Hold **Shift** while clicking and Lyra snaps the VFO to the
strongest spectrum peak within ±200 Hz of the cursor, provided that
peak is at least 6 dB above the rolling noise floor.  If no peak
is found inside the snap window, the click falls through to literal
click-to-tune (cursor freq).

Snap is sub-bin precise — parabolic interpolation around the peak
bin gives placement accuracy of ~10–20 Hz at typical FFT bin
widths (50–100 Hz at 192 kHz IQ).  In practice you click *near*
the bump on the panadapter and the radio centers exactly on the
signal.

#### Hover preview reticle

While Shift is held and you move the cursor over the spectrum, a
small cyan reticle appears showing where the next click will snap
to:

```
                            ⊥             ← snap target tick
                          [ + ]            ← crosshair at bottom
                          +180             ← Hz offset from VFO
```

The reticle disappears when no peak is in range — visual
confirmation that a Shift+click here would behave as a literal
click instead.

The reticle can be turned off (Settings → Spectrum, forthcoming);
the snap behaviour stays usable without it.

### Drag-to-pan

**Left-click and hold, then drag horizontally** to pan the panadapter
across a band — drag from one end of 40m to the other in one gesture
without touching the freq display.  The VFO follows the cursor as
you drag; on release the new center sticks.  A click that doesn't
move past a small dead-zone (5 px) is treated as a click-to-tune
instead, so you don't have to be careful about not micro-jittering.

Drag-to-pan works on the QPainter and GPU panadapter backends
identically.

### Choosing between modes

| Use case | What to do |
|---|---|
| Tune to exactly where I clicked | **Plain left-click** |
| Tune to that signal I'm pointing at | **Shift + left-click** |
| Sweep the whole band | **Left-click and drag** |

Snap is most useful on busy bands with discrete signals (CW,
digital).  On AM / FM / wide-bandwidth signals snap finds the
spectrum peak which may not be where you actually want to be (you
probably want the carrier, not the modulation peak) — use plain
click for those.

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
- **Over a notch rectangle** — adjusts that notch's width. Up =
  narrower (lower Hz), down = wider. 15% per click. See the
  [Notch Filters](notches.md) topic for full details.

## Draggable overlays

- **Passband edges** (dashed cyan lines on either side of the
  passband rect) — grab and drag horizontally to adjust the current
  mode's RX BW on the fly. The Settings → Mode + Filter BW combo
  updates live.
- **Notch rectangles** — left-drag vertically over a notch to
  fine-tune its width. Drag up = narrower; down = wider.
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

- **Spectrum:** −140 to −50 dBFS
- **Waterfall:** −140 to −60 dBFS

Moving a slider updates the display in real time. Span is clamped to
≥ 3 dB so you can't accidentally collapse the trace to a flat line
by crossing the min/max over. A **Reset to defaults** button restores
the factory values.

For ad-hoc adjustment during operating, the Y-axis drag on the right
edge of the spectrum (above) is usually faster than opening Settings.

If signals are slamming the top of the scale, either raise `max` a
few dB to see detail above the peaks, or reduce RF gain via the LNA
slider on the [DSP & AUDIO panel](panel:dsp). **Or** turn on Auto
range scaling (next section) and let Lyra fit the scale for you.

## Spectrum calibration — what `0 dBFS` means

Lyra's FFT is normalized for **true dBFS**: a unit-amplitude
full-scale sinusoid landing on the matching FFT bin reads exactly
**0 dBFS** at the bin peak. The noise floor on a quiet HF band
typically lands somewhere between −130 and −120 dBFS depending on
LNA setting, antenna, and band conditions.

**If you upgraded from an earlier Lyra build**, your spectrum used
to read about **34 dB hotter** because the old normalization summed
window-squared (a PSD-style normalization that's off by the
window's coherent-gain² factor). On first launch after the upgrade
the noise floor will appear lower on the Y-axis than you remember
— that's correct now, not a bug. Lyra automatically migrates your
saved dB-range slider positions on first launch (any saved range
whose top edge is above −45 dBFS gets shifted down 34 dB) so the
visual scale stays continuous.

If your saved range *doesn't* migrate cleanly (rare), the **Reset
to defaults** button restores the factory range.

### Spectrum cal trim — Settings → Visuals → Spectrum calibration

A single slider (range −40…+40 dB, default 0) adds an offset to
every spectrum bin before display. Use it to compensate for known
losses in the path between antenna and ADC that the FFT math can't
know about:

- **Preselector / front-end filter insertion loss** (typical ~2–4 dB)
- **Antenna efficiency** vs. an isotropic reference
- **Cable / connector loss** (significant on UHF, less so on HF)
- **Cal against a known-amplitude signal generator** through the
  full chain

Bumping the cal up by, say, +6 dB shifts every bin in the panadapter
up by 6 dB — useful when you've measured your path loss with a
signal generator and want the on-air readings to reflect dBFS at
the antenna instead of dBFS at the ADC.

**Tips:**
- **Double-click** the cal slider to snap it back to 0.
- Cal interacts with the S-meter — the dBm-equivalent calibration
  was tuned against `cal = 0`. If you set a non-zero cal trim, the
  S-meter reading shifts by the same amount (signals get reported
  as proportionally stronger / weaker in dBm).
- If you don't have measured path loss for your station, leave cal
  at 0 — that's pure theoretical dBFS at the ADC, and it's still
  internally consistent for relative measurements (a 10 dB
  improvement in noise floor is still 10 dB regardless of cal).

## Auto range scaling

A checkbox at the bottom of **Settings → Visuals → Signal range**:
**"Auto range scaling (spectrum dB scale fits to band)"**.

When on, the spectrum dB range continuously fits to current band
conditions:

- **Low edge** = noise floor − 15 dB
- **High edge** = strongest peak (rolling 10 sec) + 15 dB
- **At least 50 dB total span** guaranteed

Updates every ~2 seconds. Eliminates the manual "drag the Y-axis
every time I switch from a quiet 30m to a noisy 40m" workflow.

### Rolling-max ceiling

Critically, the high edge is the strongest peak across the **last
~10 seconds**, not just the current frame. Without this, a strong
intermittent signal would briefly spike above the recently-fitted
top, then the next auto-fit would catch up — producing the visible
"peaks at top edge / off-scale on stronger hits" symptom. The
rolling window keeps the ceiling raised until the spike is ~10 sec
old, so transients have comfortable headroom.

### Manual range = bounds, not disable

Manual dB-scale adjustments (Settings sliders, the panadapter's
right-edge Y-axis drag) **DO NOT turn auto scaling off**. Instead,
they set the **bounds** that auto-scale is allowed to operate
within:

- Drag the Y-axis ceiling down to −40 dBFS → auto-scale will never
  pull the top above −40, even if a strong transient appears
- Drag the floor up to −135 dBFS → auto-scale will never push the
  bottom below −135, even on a very quiet band

Auto-scale still continuously fits within whatever window you've
set, so you get the best of both: your preferred ceiling/floor as
hard limits, plus the auto loop tracking band conditions inside
those limits.

The **only** thing that toggles auto-scale on/off is the checkbox
itself.

### Per-band bounds memory

Spectrum range bounds are saved **per band**. When you change band
(via the Band panel buttons or by tuning across a band edge), Lyra
restores the bounds you last set for that band, OR a sensible
factory default for that band's typical noise environment if
you've never set bounds for it:

| Band group | Factory default range |
|---|---|
| 160m–60m–40m | −130 to −30 dBFS (noisy, atmospheric) |
| 30m–20m–17m | −135 to −40 dBFS (mid-HF) |
| 15m–12m–10m | −140 to −50 dBFS (quieter upper HF) |
| 6m | −145 to −55 dBFS (quietest) |

So your 40m bounds won't follow you to 6m. Drag the Y-axis on 6m
to fit a weak meteor-scatter ping → that becomes your 6m bounds.
Switch to 40m → 40m's bounds are restored. Switch back to 6m →
your meteor-scatter bounds come back.

This is automatic — no setup, no per-band UI to configure.
Operators who don't care about per-band tuning never notice;
operators who do get exactly what they want.

### When to use

- **Use auto** when band-hopping a lot, when conditions are
  changing during the day, or when you don't want to think about
  the scale at all. Per-band bounds + auto = "set it and forget
  it" across the entire HF spectrum.
- **Use manual** (uncheck auto) when comparing signal strengths
  over time and you want the scale absolutely locked, or when
  you want a specific custom range that doesn't fit any auto-fit
  algorithm.

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

## Graphics backend — Software / OpenGL / GPU panadapter

**Settings → Visuals → Graphics backend** picks how the trace +
waterfall get drawn. Four options, in order of how much of the
work the GPU does:

- **Software (QPainter on CPU)** — always works, no GPU involved.
  Every Windows machine from the last 20 years runs it. The safe
  fallback if anything else gives you trouble.
- **OpenGL — accelerated QPainter** — same QPainter code, but with
  a `QOpenGLWidget` base so rasterization happens on the GPU.
  Smoother resize / fullscreen, reduces audio stutter on weaker
  CPUs. **Recommended for most operators.** Restart required.
- **GPU panadapter (beta — opt-in)** — *new in v0.0.5+.* Custom
  OpenGL pipeline written from scratch: vertex-buffer trace + texture-
  streaming waterfall via custom GLSL shaders against an OpenGL 4.3
  core context. Fastest path; the panadapter feels noticeably
  smoother than the QPainter widgets even on already-fast hardware.
  See **GPU panadapter (beta)** below for what's working and what
  isn't yet.
- **Vulkan (future, not implemented)** — placeholder. We may revisit
  if PySide6's QRhi bindings mature enough to make a Vulkan path
  worth the work, or if a real performance need surfaces that
  OpenGL can't satisfy. Today, neither is true.

If your selected backend fails to initialize for any reason (bad
driver, remote session, headless CI, broken shader), Lyra silently
falls back to Software. The Visuals tab shows which backend is
actually live alongside the one you selected.

### GPU panadapter (beta)

The new GPU panadapter renders the trace as a single GPU draw call
(one `glDrawArrays` instead of one `drawLine` per pixel column) and
streams the waterfall into a 2D texture so each new row is a
single `glTexSubImage2D` upload, no buffer scrolling. The result:
the panadapter feels smoother than even the OpenGL-accelerated
QPainter version on the same hardware.

**What works in v0.0.5** (full feature parity with the QPainter widget):

- Trace + waterfall render with full color palette + live Settings
- VFO marker, CW Zero reference line
- Click-to-tune on the trace and waterfall
- Right-click context menu (notch quick-add, etc.)
- Wheel-to-zoom on empty spectrum
- Y-axis drag to set the spectrum dB range
- Passband overlay (cyan rectangle showing RX filter window)
- RX-BW resize via dragging the passband edges
- Notch markers + drag-to-resize-width + right-click menu
- Spot markers (callsign boxes with multi-row collision packing
  and age-fade)
- Band-plan strip (CW/DIG/SSB segments + landmark click-to-tune
  triangles + red dashed band-edge warnings)
- Peak-hold markers (line / dots / triangles, optional dB readout)
- Noise-floor reference line
- Grid toggle (operator preference)

The QPainter widget remains available indefinitely as the safety-net
fallback. Switch backends in **Settings → Visuals → Graphics backend**.

**System requirements:** the GPU panadapter needs an OpenGL 4.3
core profile context. That covers every NVIDIA / AMD / Intel GPU
made since approximately 2013 — essentially any machine running a
current Windows 10 or 11 install with up-to-date drivers.

## Update rates and zoom

Three independent controls in **Settings → Visuals → Update rates
and zoom**. Changes are live — no restart needed. Front-panel **DISPLAY**
strip (Zoom / Spec / WF sliders) mirrors these settings both ways —
adjust in either place, the other updates.

### Panadapter zoom

Crops the FFT to a centered subset of bins so the panadapter
magnifies around your RX frequency. Levels: **1× / 2× / 4× / 8× /
16×**. No impact on DSP, demod, or recorded IQ — it's purely a
display change.

Two ways to switch:
- **Mouse wheel** on empty spectrum (not over a notch rectangle) — each
  tick steps one level. Up = zoom in, down = zoom out.
- **Settings → Visuals → Panadapter zoom** combo, or the front-panel
  **DISPLAY** Zoom slider.

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
