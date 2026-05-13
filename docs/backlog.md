# Lyra Backlog

## ⭐ Headline features (from original project spec)

- [/] **Neural / AI Noise Reduction** — original day-one spec item.
      **Phase 1 (classical spectral subtraction) shipped 2026-04-23**
      and was superseded in **v0.0.9.6 by WDSP EMNR Mode 1-4 + AEPF +
      NPE** (Warren Pratt's spectral-subtraction engine ported via
      cffi), which is the production NR engine today.  See
      `lyra/dsp/wdsp_engine.py` and `docs/help/nr.md`.
      The neural path remains the **Phase 2** plan — a **Neural**
      slot is reserved in the NR right-click profile menu, greyed
      out until a neural package is importable (detection via
      `Radio.neural_nr_available()` trying `rnnoise_wrapper` and
      `deepfilternet` as import targets).

      **Phase 2 — remaining work for neural integration:**
      - **RNNoise** (Jean-Marc Valin / Mozilla) — small RNN, real-time
        (~1% CPU), designed for speech NR. Ship as optional dep via
        `pip install rnnoise-wrapper` or similar. Best speech-on-noise
        quality per watt.
      - **DeepFilterNet** — Rust+Python, larger model, even better
        quality than RNNoise; ~5% CPU on a modern laptop. Real-time.
      - **NVIDIA RTX Voice / Krisp** — heavy, GPU-accelerated, huge
        model files. Skip.

      **Integration pattern when ready:**
      - `Radio.set_nr_profile("neural")` should instantiate a neural
        processor (wrapping RNNoise / DFN) and route audio through it
        instead of the classical `SpectralSubtractionNR`.
      - Keep classical path as fallback if neural init fails at
        runtime (missing model file, hardware issue, etc.).
      - Neural NR operates on 48 kHz mono, same contract as classical;
        no pipeline changes needed.
- [ ] **Pre-distortion (PureSignal-equivalent)** — the other day-one TX
      spec item; reduces IMD products dramatically for clean wideband
      TX. Big DSP lift. After basic TX is working.



Features requested by user or found as gaps during development.
Ordered roughly by priority. Not a contract — reorder freely.

## Modes still to add (the reference HPSDR client parity)

- [ ] **SAM** (Synchronous AM) — PLL locks to carrier, then real-part
      demod of the phase-aligned baseband. Reduces fade distortion on AM.
- [ ] **DRM** (Digital Radio Mondiale) — specialized decoder; probably
      integrate an external DRM library rather than implement from scratch.
- [ ] **AM_LSB** — AM with LSB only (carrier-restored, one sideband).
- [ ] **AM_USB** — same, USB side.
- [ ] **SPEC** — panadapter-only mode (no audio). Currently equivalent
      to "Off" in Lyra.

Already added (2026-04-21): LSB, USB, CWL, CWU, DSB, AM, FM, DIGU, DIGL.

## RX polish (next)

- [x] **Audio stutter on resize / fullscreen** — largely addressed
      2026-04-23 by making SpectrumWidget / WaterfallWidget inherit
      from `QOpenGLWidget` when the user picks "OpenGL" in
      Settings → Visuals → Graphics backend. Default remains Software
      for universal compatibility. Threaded demod is still on the
      table as a belt-and-suspenders improvement, especially if any
      user reports stutter persisting on OpenGL.
- [ ] **RX filter edge shift** — currently presets only. Add user-draggable
      edges on the spectrum (translucent passband overlay) + mouse-wheel
      shift keys for Lo/Hi cutoff (same idiom as per-notch Q).
- [ ] **TX filter** — currently just a stored value. Wire it to the TX
      modulator once TX path is built.
- [x] **Lit-Arc S-meter** — shipped as lit-amber arc on black dial
      (classic Kenwood/Yaesu aesthetic, matching the reference photo).
      In v0.1 §7.1 polish, the original "Analog needle" variant was
      retired in favor of just two styles: Lit-Arc and LED-bar.
- [x] **LED multi-scale bar-graph meter mode** — shipped as the LED-bar
      alternative meter style.  Switch via the chip-row in the Meters
      panel header (not right-click).  Will share the same multi-meter
      data feed (S-meter for RX, Power+SWR+ALC for TX) once TX lands.

## DSP — EESDR3-parity RX audio chain

Source: ExpertSDR3 manual pp. 70-95. Full feature set for eventual parity.

- [ ] **AGC presets** (FAST / NORM / OFF) + per-mode Attack / Decay / Hang
      time in ms. Currently we have a single fixed-attack peak-tracker.
- [ ] **Filter taps per mode** — default 1500, user-settable. Higher taps
      = sharper filter slopes but more audio delay. Currently hardcoded 255.
- [ ] **4 user-named filter BW presets per mode** with custom Low / High
      edges (not just fixed preset list). reference SDR clients style.
- [ ] **Noise Blanker (NB)** — detect → suppress → interpolate impulse
      noise. Threshold + pulse-width adjustable. Pre-demod (I/Q domain).
- [ ] **Noise Reduction (NR)** — adaptive spectral subtraction with
      Quality (speed of adaptation). Post-demod audio domain.
- [ ] **ANC** — neural-network stationary-noise suppression (3 presets).
      Probably defer or use a simpler Wiener filter initially.
- [ ] **BIN** (binaural pseudo-stereo) — phase-shift trick for SSB spread.
      Requires stereo output support.
- [ ] **Equalizer — TRUE parametric (mandatory bypass mode)**. NOT a
      fixed-band graphic EQ. Each band: frequency, gain (dB), Q (width),
      and filter type (peak / low-shelf / high-shelf / low-cut / high-cut).
      Separate EQ chains for RX audio and TX audio. User-nameable
      presets, per-mode defaults. Must include **"EQ Off"** straight-
      through mode as the default.
      References: EESDR3 RX/TX equalizer (multiple filter types + per-mode
      presets); the reference HPSDR client RX/TX equalizer for chain architecture and preset
      UI. Pull specific designs before implementation. **Do not ship a
      simple 5-band fixed-freq graphic EQ — users expect parametric.**
- [ ] **Loudness compensation** — frequency tilt presets (age groups per
      EESDR3, but could be just "flat / bass / treble / voice").
- [ ] **Audio driver selection** — let user pick MME / WDM-KS / WASAPI /
      ASIO on Windows for lowest latency. Currently `sounddevice` default.

## DSP — originally listed

- [ ] **AGC profiles** (Off / Slow / Fast / Custom) — conventional with
      attack/decay/threshold exposed.
- [x] **LNA auto-adjust** — shipped 2026-04-23. Auto button next to
      the LNA slider on the DSP & AUDIO panel. 1.5 s control loop
      monitors IQ peak via `_lna_peaks` (90th-percentile, robust to
      transients), targets −15 dBFS with ±3 dB deadband, ±3 dB per-
      step clamp. User can still drag the slider to override.
- [ ] **Noise Blanker** (impulse suppression) — EESDR3 has NB with
      pulse-width + threshold. Applied in I/Q before demod.
- [ ] **Noise Reduction** (NR) — spectral subtraction / Wiener. Probably
      the "neural AI NR" the user mentioned long-term.
- [ ] **Auto-notch (ANF)** — LMS-style auto-notch for CW interference.
- [ ] **Binaural (BIN)** — stereo spread for SSB (EESDR3 DSE).

## Spectrum / waterfall controls (user request 2026-04-21)

- [x] **Waterfall scroll rate / FPS** — shipped 2026-04-23 as the
      "Waterfall rate" slider in Settings → Visuals → Update rates
      and zoom. Decoupled from spectrum FPS via an internal push
      divider; UI shows rows/sec derived from divider × spectrum FPS.
- [x] **Panadapter FFT rate** — shipped 2026-04-23 as the "Spectrum
      rate" slider (5 – 60 fps). Averaging is still on the backlog —
      see separate entry.
- [ ] **FFT averaging** — option to average N FFTs before emitting
      spectrum_ready for a smoother trace (low-signal viewing). Orthogonal
      to the FPS slider that's already shipped.
- [x] **Noise-floor baseline marker** — shipped 2026-04-24. Dashed
      sage-green horizontal line + `NF −NN dBFS` label, computed as
      20th-percentile FFT rolling-averaged over ~1 s with additional
      EMA smoothing. Toggle in Settings → Visuals; emits at ~6 Hz
      to keep the line rock-steady. Same underlying math feeds AGC's
      auto-threshold calibration.
- [x] **Peak / min dB adjust** — shipped 2026-04-23 as four sliders
      in Settings → Visuals → Signal range (spectrum min/max +
      waterfall min/max, each −150 … 0 dBFS, span clamped ≥ 3 dB).
      Live-apply; persists to QSettings.
- [ ] **Auto-range button** — one-click fit to current signal range.

## Spectrum / waterfall visuals

- [x] **Translucent filter passband overlay** on spectrum — shipped
      2026-04-24. Cyan translucent rect + dashed edges showing RX
      filter window. Updates live on mode / RX BW change.
- [ ] **Band-indicator strip** at top of panadapter (colored stripes per
      ham band, similar to EESDR3).
- [x] **Zoom** — mouse wheel on spectrum = step through zoom levels
      (1× / 2× / 4× / 8× / 16×) when not over a notch tick; wheel over
      a notch still adjusts Q. Shipped 2026-04-23; combo selector in
      Settings → Visuals → Update rates and zoom.
- [ ] **Pan** — click-drag on spectrum should pan the center
      frequency. Not yet implemented; hover-wheel over notch does
      Q, which is the conflicting gesture to resolve.
- [?] **Panadapter scaling** — user flagged end of 2026-04-24 session;
      exact intent TBD. Candidates to ask:
      (a) dB Y-axis scaling mode — linear vs log visualization
          (we're already dBFS, so "log" is implicit; maybe user
          means power-spectrum vs magnitude-spectrum visual weight)
      (b) Auto-range button / hotkey to fit current signal envelope
          into the current dB range
      (c) Y-axis tick density — finer / coarser grid lines
      (d) Freq-axis non-linear scaling (log-freq across a band for
          panoramic view at wide rates — unlikely at HL2 spans)
      **Ask on session start: which of these four did you mean?**
- [?] **Noise-floor line color — user-pickable** — currently the NF
      reference line is hardcoded sage green (120/200/140/160 RGBA).
      Add it to the color-picker row in Settings → Visuals the same
      way as the trace + segment colors. Trivial: one swatch, one
      property, one subscribe. Ask: do you want a separate NF color
      or should it track the spectrum-trace color with muted alpha?
- [!] **AK4951 routing broken — audio plays after jack unplugged** —
      reported end of 2026-04-24 session. With AK4951 selected as
      Output, user yanked the physical jack on the HL2+ AK4951 board
      and audio kept playing. Means the software-thinks-AK4951-but-
      it's-actually-PC-SoundDevice condition. Candidates to
      investigate tomorrow (in priority order):
      1. `SoundDeviceSink.close()` — does PortAudio actually release
         cleanly on Windows WASAPI? Try explicit `abort()` before
         `stop()` + `close()`. The lingering stream theory is
         strongest.
      2. `set_audio_output` sink-swap — verify we're not leaving a
         stale `_audio_sink` reference somewhere (e.g. a captured
         closure, a bound Qt signal) that keeps the old sink alive
         even after reassignment.
      3. Verify `AK4951Sink` is actually writing bytes — add a
         debug counter that logs bytes-per-second through
         `queue_tx_audio` when AK4951 is active. If zero while PC
         audio still works, the switch never took.
      4. Check whether `inject_audio_tx` is actually being set on
         the live stream instance (not a stale one). Print the id()
         of the stream in both `_stream_cb` and `AK4951Sink.__init__`
         to confirm they match.
      5. Rule out: user's Windows audio device isn't a Virtual
         Cable or VB-Cable that's holding the stream open from
         outside Lyra's control.
      **Diagnostic plan**: on next session start, instrument both
      sinks with a "bytes written" counter visible in status bar, so
      the active sink is verifiable from the UI.

- [?] **Faster waterfall "to the touch"** — user flagged end of
      2026-04-24; exact intent TBD. Candidates to ask:
      (a) Extend max multiplier beyond current 3× (e.g., 5× or 8×).
          At 30 fps × 5 = 150 rows/sec waterfall.
      (b) Bump spectrum FPS cap from 60 → 120, which naturally
          raises the max waterfall rate (WF = FPS ÷ divider).
      (c) Reduce input-to-paint latency — waterfall currently
          pushes a row per `waterfall_ready` signal (IO-thread →
          main-thread hop). Could skip a round-trip by pushing
          via shared memory or Qt Quick Scene Graph.
      (d) Smooth-scroll the waterfall (pixel-accurate slide between
          FFT rows) instead of hard snaps — feels faster/smoother
          without actually computing more FFTs.
      **Ask on session start: which of these four feels right?**
- [?] **Peak-markers render style option** — currently peak hold
      draws a 1.3 px amber line. User asked about "dot markers"
      instead — like Icom's point-at-each-bin spectrum style.
      Settings → Visuals → Peak markers section, add a combo:
      Line / Dots / Outlined dots / Stepped bars. Ask which 2-3
      styles are worth shipping. May also want "show-peaks-only"
      mode where the peak trace renders only at bins > N dB above
      current live signal (i.e. genuine peak markers vs a ghost
      trace).
- [ ] **Click-and-drag to tune** (EESDR v3 style) — left-click-and-
      hold on the spectrum, drag horizontally, and the RX freq
      follows the cursor in real-time. Distinct from spectrum pan
      (which moves the visible window); this moves the *tune center*
      so the operator can nudge smoothly across a busy band instead
      of click-teleporting. Potential gesture-conflict resolution:
      modifier key (Shift+drag = tune, unmodified drag = pan), or
      tune-drag only engages on the center ±5 % of the widget so
      notch-drag and edge-drag stay clean.
- [ ] **Spot cluster coalesce** (anti-clutter B) — when more than N
      spots land inside a ~30 px horizontal bucket, collapse them
      into a single "+N more" badge instead of piling up rows. Low
      priority — current 4-row collision stack + age-fade + mode
      filter handle typical FT8 density fine. Valuable on
      contest-weekend pileups.
- [x] **Configurable waterfall palette** — shipped 2026-04-23 with
      7 built-in palettes in Settings → Visuals → Waterfall palette
      (the reference HPSDR client / Inferno / Viridis / Classic / Ocean / Night / Grayscale).
      Live switch; persists across launches.
- [ ] **Markers / "blobs"** — labeled markers for stations, CW beacons,
      last-heard, etc. some reference clients have these.

## Transceiver & protocol

- [x] **Full-duplex** — already enabled (C4 bit 2 set in config register).
      Required for pre-distortion and for proper HL2/HL2+ PA operation.
- [ ] **TX path** — SSB modulator, PTT, CW keyer, RTTY/FSK, AK4951 mic
      input vs PC mic selectable. User's preferred mic path: AK4951.
- [ ] **PA protection** / fault monitoring — HL2 I/O board registers
      (fault, fan speed, RF inputs). See the reference HPSDR client `IoBoardHl2.cs`.
- [ ] **Pre-distortion (PureSignal equivalent)** — feedback-based IMD
      cancellation. Big lift. some reference clients have a reference impl.
- [ ] **Band-switching + per-band memory** — BandPanel with auto-tune
      to last-used freq for each band, gain and notch state per band.

## Integrations

- [ ] **TCI v1.9/v2.0 WebSocket server** — subscribe to Radio signals,
      translate to TCI commands, listen on ws://localhost:40001 for
      inbound commands. Enables log4OM, N1MM+, MixW, JS8Call, etc.
- [ ] **CAT serial / rigctl compatibility** — alternative to TCI for
      legacy software.
- [ ] **VAC (Virtual Audio Cable)** — route demodulated audio to any
      PC audio device by name (currently only default device or AK4951).

## UI / UX

- [ ] **Settings dialog** — one place for: waterfall palette + contrast,
      spectrum colors, grid opacity, font sizing, S-meter calibration,
      AGC profile defaults, startup freq/mode/band, TCI server port,
      audio output device name, fonts.
- [ ] **Multiple themes / user-editable palette** — the reference HPSDR client and EESDR3
      both allow per-element color picking.
- [ ] **Dockable panels** — rearrangeable layout via Qt dock widgets
      so the operator can hide panels they don't use.
      User also wants: S-meter (and other meters) draggable out of the
      main panel and resizable independently, like EESDR3's popout
      analog meter window. Convert GlassPanel hierarchy to
      `QDockWidget` hosting; each dock can float, tear off, or snap
      back. Keep existing signal wiring — only containment changes.
- [x] **Graphics driver: OpenGL / Vulkan backend** — OpenGL shipped
      2026-04-23. `lyra/ui/gfx.py` picks the widget base class at
      import time from Settings → Visuals → Graphics backend
      (Software / OpenGL; Vulkan listed but disabled). OpenGL path
      uses Qt's GL-accelerated QPainter, so same paint code, just
      GPU rasterization. Requires restart on backend change; UI
      warns. Silent fallback to Software if GL context fails.
      Vulkan on Qt is `QVulkanWindow` — secondary
      target once OpenGL version works.

## Operator workflow

- [ ] **RX / TX profiles** — named, one-click preset bundles that
      capture the full operating configuration for a given activity.
      Orthogonal to per-band memory (which is automatic, band-scoped);
      profiles are manual, activity-scoped, and portable (export /
      import as JSON).

      **RX profile snapshot captures**:
      - Mode + RX BW (filter width)
      - AGC profile + threshold + custom release/hang
      - LNA gain + Auto-LNA on/off
      - DSP button states: NB, BIN, NR, ANF, APF, NF
      - Notch list (frequencies as offsets from carrier, Q values)
      - Volume + mute state
      - Audio output device
      - Spectrum / waterfall dB range, palette, zoom, FPS (optional
        — many operators want a persistent "view" across profiles)

      **TX profile snapshot captures** (when TX path lands):
      - TX BW + mic source (AK4951 vs PC mic)
      - Mic gain, compression / speech-processor settings
      - Monitor level, VOX params
      - CW keyer speed / weight / iambic mode
      - Parametric EQ curve
      - Drive power, PA protection thresholds

      **Typical profiles operators would build**:
      `40m FT8` · `20m SSB ragchew` · `CW contest` · `DX chase` ·
      `SWL broadcast` · `WSPR receive` · `QSK CW`

      **UI sketch**: a profile combo on the toolbar + `Save As…`
      button. Right-click combo → rename / delete / export. Profile
      switch is non-destructive — switching back restores exactly.

      **Ties in with**:
      - Parametric EQ presets (already backlogged, natural profile
        member when EQ lands)
      - TCI (could let logging software switch the rig's profile as
        the operator changes activity)
      - Per-band memory stays as the automatic layer underneath

## Infrastructure

- [ ] **CLI test harness** — headless `tools/test_radio.py` that runs a
      scripted session (tune, record, notch, demod) and checks outputs.
      Lets us catch regressions without the UI.
- [ ] **Per-session log** — human-readable log of freq changes, mode
      switches, TCI commands in/out. Aids debugging.

## Known quirks / gotchas

- HL2 baseband spectrum is mirrored — USB signals appear in negative
  baseband freqs. Handled in SSBDemod via sign flip.
- Duplex bit must be set or RX freq is ignored. Handled.
- EP2 keepalive is mandatory (1:1 with EP6) or the stream halts after
  a few seconds. Handled.
- AK4951 board requires updated gateware. User has applied it.
- N8SDR station has a strong BCB 5th harmonic at 7.250 MHz — good test
  target for notch depth. See project_lyra_rf_environment memory.
