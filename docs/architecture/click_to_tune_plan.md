# Click-to-tune + drag-to-pan plan

**Status:** survey + design proposal. Implementation deferred until
operator approval.
**Date:** 2026-05-02.
**Operator request:**
> Click to tune option which lets a user click very close to a signal
> and it auto-tunes it to the frequency marker. Both behaviors with a
> switch, and we also need to be able to drag the panadapter across a
> band (example 40m end-to-end).

This document covers two related features:

1. **Click-to-tune** — clicking on the panadapter snaps the VFO to
   the click location. Two flavors:
   - **Literal** — VFO goes to the exact clicked frequency.
   - **Snap-to-peak** — VFO snaps to the nearest detected signal
     peak within a few hundred Hz of the click.
   Modifier-key switchable per click.
2. **Drag-to-pan** — clicking and holding on empty spectrum, then
   dragging horizontally, scrolls the entire panadapter view across
   the band, dragging the VFO along with it. Operator can sweep
   from 7.000 to 7.300 MHz by dragging end-to-end without touching
   the freq display.

---

## 1. Current state — what already works

Lyra's `SpectrumWidget` already has substantial click/drag
infrastructure (`lyra/ui/spectrum.py`):

- **Click-to-tune** (literal) is partially implemented:
  `_drag_tune: tuple[int, float, bool]` state machine in `mousePressEvent`
  / `mouseMoveEvent` / `mouseReleaseEvent`. A press on empty spectrum
  starts a tune-or-pan gesture; if cursor doesn't move past
  `DRAG_TUNE_THRESHOLD_PX = 5`, it's a click and emits
  `clicked_freq` to the freq panel. If it moves further, it's a pan
  gesture (drag-tune).
- **Drag-to-pan** is partially implemented: during the drag the
  panadapter visually scrolls (panel forwards proposed center freq
  to Radio). On release, the new center sticks.
- **Notch drag**, **passband edge drag**, **dB scale drag** — all
  working.
- **Spot click**, **landmark click** — working.

What's **missing**:

1. **Snap-to-peak** mode — never implemented.
2. **Modifier-key switching** between literal and snap modes.
3. **Smoother drag-pan UX** — current implementation reportedly works
   but operator wants explicit confirmation it can do "40 m end to
   end" sweep.
4. **Visual feedback** during snap mode — show the snap target with
   a hint marker before commit.

The good news: 60% of the work is already there. The plan below adds
the missing 40%.

---

## 2. Reference: how Thetis does it

From Thetis's `Console\display.cs` and `console.cs`:

- **Single left click on panadapter** → VFO jumps to clicked frequency
  (literal mode is the default).
- **Modifier (Ctrl-click)** → snap to nearest peak (search ±100 Hz
  window).
- **Click and drag** → `picPanadapterMouseDown` tracks position;
  `picPanadapterMouseMove` updates VFO continuously while button
  held.
- **Right-click drag** → moves only the spectrum view (no VFO
  change).
- **Wheel scroll on panadapter** → tunes by step size.

Thetis's snap-to-peak algorithm (rough): on click, find the maximum
spectrum bin within ±N Hz of the cursor (N = operator-set
"snap-tune" range, default 100 Hz). If the max is more than X dB
above the noise floor, snap. Otherwise tune to the literal click.

Lyra's existing click handler is close in spirit — we just need to
add the snap algorithm and wire the modifier.

---

## 3. Proposed UX

### 3.1 Default mode: literal

Plain left-click → VFO jumps to clicked frequency (current behavior).

### 3.2 Snap mode: hold modifier or toggle in settings

**Two ways to engage snap:**

- **Hold-modifier** — `Shift + click` (or `Ctrl + click`, configurable)
  triggers a snap on that click only. Status quo for everything else.
- **Toggle** — Settings → Spectrum → "Snap to peak by default"
  inverts the modifier. When toggle is on, plain click snaps and
  modifier+click is literal.

This gives operator both styles: occasional snappers hold the key;
power-user snappers turn on the toggle and use modifier as the
"escape hatch."

### 3.3 Snap algorithm

1. Read the cursor freq `f_cursor`.
2. Search the spectrum bin range `[f_cursor - W, f_cursor + W]` where
   W = "snap-tune range" (operator-configurable, default 200 Hz).
3. Find the bin with the maximum dB value in the range.
4. Compare to noise floor + threshold: if `max_db - noise_floor >
   SNAP_MIN_SNR_DB` (default 6 dB), snap to that bin's center
   frequency. Otherwise fall through to literal click.
5. Optional: parabolic interpolation around the peak bin for
   sub-bin-precision (gives ~10-20 Hz placement accuracy at typical
   FFT bin widths of 50-100 Hz).

The noise floor is already tracked by Radio (`noise_floor_changed`
signal). Snap reuses that — no new estimation needed.

### 3.4 Visual feedback

When the operator hovers the spectrum with the modifier held (or with
snap-toggle on), draw a small target reticle at the predicted snap
frequency. Updates live as the cursor moves. Helps the operator see
"yes, the snap will land me on this bump" before committing the
click.

When no peak is found in the snap window, the reticle disappears or
dims, hinting the click will be literal.

### 3.5 Drag-to-pan

The existing drag-tune is **continuous panning** — as the cursor
moves, the panadapter view follows. Confirm this works at "40 m end
to end" speed:

- 40 m band is 7.000 - 7.300 MHz = 300 kHz.
- Default zoom shows ~96 kHz (depending on rate).
- To sweep end-to-end requires roughly 3 view-widths of drag, OR
  the operator can drag the entire band into view by zooming out.

**Verify:** during a drag-tune the protocol layer commits the new
center frequency at every move event (every ~16 ms = ~60 Hz refresh).
At HL2's actual freq-write rate (~50 Hz), that means freq updates
*follow* the cursor with ~20 ms lag — barely perceptible. If it's
laggy in field test, debounce to ~30 ms (33 Hz update rate).

The reportedly working drag-tune already does this, so likely no
changes needed — but worth a confirmation flight test.

### 3.6 Wheel-to-tune (bonus)

Wheel scroll on the panadapter could tune by the operator's
configured step size (currently set in Settings → Tuning). Not
required by the operator's request, but trivial addition once we're
in the mouse-handler code.

---

## 4. Implementation outline

### 4.1 Code locations

```
lyra/ui/spectrum.py
  ├─ class SpectrumWidget (paint: software path)
  │    ├─ mousePressEvent       # already has click-vs-drag logic
  │    ├─ mouseMoveEvent        # already has drag handling
  │    ├─ mouseReleaseEvent     # already commits on release
  │    └─ NEW: _find_snap_target(x_pixel) -> Optional[float]
  └─ class SpectrumWidgetGL (paint: GPU path)
       └─ same structure

lyra/radio.py
  └─ NEW: snap_tune_range_hz, snap_tune_min_snr_db (settings)
  └─ NEW: snap_tune_default_on (settings — invert modifier)

lyra/ui/settings_dialog.py
  └─ NEW: Spectrum tab → snap-tune section (range, threshold,
                                            modifier choice, default
                                            on/off, visual reticle
                                            on/off)
```

### 4.2 The snap function

```python
def _find_snap_target(self, x_pixel: int) -> Optional[float]:
    """Given the cursor x-pixel, find the strongest spectrum bin
    within ±snap_range_hz of the cursor frequency. Return its
    center frequency, or None if no significant peak is found."""
    if self._spec_db is None or len(self._spec_db) < 2:
        return None
    f_cursor = self._x_to_freq(x_pixel)
    range_hz = self._snap_tune_range_hz
    f_lo = f_cursor - range_hz
    f_hi = f_cursor + range_hz
    bin_lo = max(0, self._freq_to_bin(f_lo))
    bin_hi = min(len(self._spec_db), self._freq_to_bin(f_hi) + 1)
    if bin_hi <= bin_lo:
        return None
    window = self._spec_db[bin_lo:bin_hi]
    peak_idx = int(np.argmax(window)) + bin_lo
    peak_db = float(self._spec_db[peak_idx])
    # Compare against the rolling noise floor.
    if (peak_db - self._noise_floor_db) < self._snap_min_snr_db:
        return None
    # Optional: parabolic peak interpolation for sub-bin precision.
    if 0 < peak_idx < len(self._spec_db) - 1:
        y0 = self._spec_db[peak_idx - 1]
        y1 = self._spec_db[peak_idx]
        y2 = self._spec_db[peak_idx + 1]
        denom = y0 - 2*y1 + y2
        delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-12 else 0.0
        peak_idx_refined = peak_idx + delta
    else:
        peak_idx_refined = float(peak_idx)
    return self._bin_to_freq(peak_idx_refined)
```

### 4.3 The mouse handler change

In `mouseReleaseEvent`, when committing a click (not a drag):

```python
shift_held = bool(event.modifiers() & Qt.ShiftModifier)
snap_default = self._snap_tune_default_on  # operator preference
should_snap = snap_default ^ shift_held    # XOR — modifier inverts default

if should_snap:
    target = self._find_snap_target(event.position().x())
    if target is not None:
        self.clicked_freq.emit(target)
        return
    # Fall through to literal if no peak found.

# Literal mode (or snap-no-target).
freq = self._x_to_freq(event.position().x())
self.clicked_freq.emit(freq)
```

### 4.4 The hover reticle

In `mouseMoveEvent` (only when not actively dragging):

```python
shift_held = bool(event.modifiers() & Qt.ShiftModifier)
snap_default = self._snap_tune_default_on
showing_snap_hint = snap_default ^ shift_held

if showing_snap_hint and self._show_snap_reticle:
    target = self._find_snap_target(event.position().x())
    self._snap_hover_freq = target  # None or float
    self.update()  # repaint to show/hide reticle
```

In `paintEvent`, draw a small reticle (crosshair, dot, or pill) at
`self._snap_hover_freq` if non-None.

### 4.5 Settings persistence

QSettings keys:
- `spectrum/snap_tune_range_hz` (int, default 200)
- `spectrum/snap_tune_min_snr_db` (float, default 6.0)
- `spectrum/snap_tune_modifier` (string: "shift" | "ctrl" | "alt", default "shift")
- `spectrum/snap_tune_default_on` (bool, default False)
- `spectrum/snap_tune_show_reticle` (bool, default True)

---

## 5. Drag-to-pan: confirmation + tuning

The existing drag-tune already follows the cursor. Field-test items:

1. **End-to-end test:** start at 7.000 MHz, drag-pan continuously to
   7.300 MHz across the panadapter. Confirm:
   - VFO updates continuously (no lag).
   - Spectrum view scrolls smoothly.
   - HL2 keeps up with the freq writes (no audio dropouts).

2. **Optional refinement: pan-only mode.** If the operator wants to
   move the spectrum without changing VFO, hold a modifier (Right-
   click drag, or Ctrl+drag). Match Thetis. Default: drag tunes the
   VFO; modifier-drag pans the view.

3. **Wheel-zoom interaction:** wheel scroll on the panadapter
   currently does what? — verify; if it doesn't already zoom, add
   that. Operator may want wheel-scroll = zoom and drag = pan as
   the natural pairing.

4. **Drag-pan beyond visible band.** Should the panadapter let the
   operator drag past 40 m's edges into 30 m? Probably yes — the
   freq display will simply pin to the next valid frequency. No
   special handling needed.

---

## 6. UI sketch

```
Settings → Spectrum tab
  ┌─────────────────────────────────────────────┐
  │ Click-to-tune                               │
  │                                             │
  │  Snap to peak by default       [□ off]      │
  │                                             │
  │  Snap-tune range               [200 Hz ▼]   │
  │                                                  range: 50 - 1000 Hz
  │  Minimum peak above noise      [ 6.0 dB ▼]   │
  │                                                  range: 3.0 - 20 dB
  │  Snap modifier key             [Shift ▼]    │
  │                                                  options: Shift / Ctrl / Alt
  │  Show snap reticle on hover    [✓ on]       │
  │                                             │
  │ Drag-to-pan                                 │
  │                                             │
  │  Drag pans the spectrum         (info text) │
  │  Hold Right-click or Ctrl+drag              │
  │  to pan view without changing VFO           │
  └─────────────────────────────────────────────┘
```

Snap reticle visual: a small ⊕ symbol drawn at the snap target,
slightly transparent, in the spectrum's accent color. Updates live
as the cursor moves; disappears when no peak is in range.

---

## 7. Edge cases

1. **Click in a CW pileup with multiple close peaks.** Snap finds the
   strongest in the search window. If the operator wanted a weaker
   one nearby, they should narrow the search window or click closer
   to the target.
2. **Click in a wide-bandwidth signal (AM, FM).** Snap finds the
   spectrum *peak* — for a CW carrier this is the carrier; for an
   AM signal it's the carrier; for FM it could be anywhere depending
   on modulation. Operator should turn off snap (or use modifier) on
   broadband modes.
3. **Click during waterfall scroll.** No interaction — waterfall is
   visual only; click hits the spectrum/waterfall as currently
   handled.
4. **Click on a notch handle.** Existing behavior (notch
   manipulation) takes precedence over click-to-tune. No change.
5. **Click on a passband edge.** Existing behavior (passband edge
   drag) takes precedence. No change.
6. **Click on a DX spot box.** Existing behavior (`spot_clicked`)
   takes precedence. No change.

The mouse-event chain has a clear precedence order already (notch >
passband > spot > landmark > tune); snap fits into the tune slot, so
no precedence rework needed.

---

## 8. Recommended phasing

| Phase | Scope | Effort | Risk |
|---|---|---|---|
| 1 | Snap algorithm + Shift+click for snap, plain click stays literal | 1 day | Low |
| 2 | Settings tab additions + persistence | 4 hours | Low |
| 3 | Hover reticle visualization | 4 hours | Low |
| 4 | Drag-pan field test (likely no code) | 1 hour | None |
| 5 | (Optional) Right-click drag = pan-without-VFO | 2 hours | Low |
| 6 | (Optional) Wheel-to-zoom or wheel-to-tune | 2 hours | Low |

**Recommendation:** ship phases 1-3 as one feature ("click-to-tune
v1"). Phases 4-6 ship as polish in the same release if time permits.

This is small enough to slot before v0.0.9 RX2 work — could ship as
v0.0.7.x alongside the audio-pop and notch-filter improvements.

---

## 9. Bench / field test plan

1. **Snap accuracy test.** Generate a synthetic spectrum with a peak
   at a known frequency (say 7.040 MHz). Programmatically click at
   `peak ± offset` for offset ∈ {0, 50, 100, 200, 500} Hz. Verify
   snap returns to the true peak for offset ≤ snap_range_hz, and
   falls through to literal otherwise.
2. **Snap noise-floor test.** Synthesize spectrum with peak at known
   SNR ∈ {3, 6, 10, 20 dB}. Verify snap engages only when SNR
   exceeds the threshold setting.
3. **Drag-pan test.** Start at 7.000 MHz, drag to 7.300 MHz with
   timing. Verify VFO updates fire at >30 Hz (smooth) and HL2
   never drops audio.
4. **Operator A/B.** Set up a busy band, ask operator to tune to 5
   weak signals using a) literal click only, b) snap mode. Time
   each. Snap should be notably faster on weak-but-present signals.

---

## 10. Out of scope for this plan

- **Hover-frequency readout** — Lyra already shows the cursor freq.
- **Spectrum / waterfall zoom changes** — separate UI concern.
- **Memory channels / band stack** — separate features.
- **TCI integration** — TCI already pushes freq changes; no
  click-to-tune-via-TCI change needed.

---

## 11. Approval gate

Before any code change:

1. Operator confirms the snap defaults look right (200 Hz range,
   6 dB threshold, Shift modifier).
2. Operator confirms the snap-vs-literal toggle UX (modifier
   inverts the default).
3. Operator runs a quick drag-pan test today and reports if the
   existing drag works "end-to-end" or feels laggy.
4. We open a feature branch and ship phases 1-3 as
   click-to-tune v1.
