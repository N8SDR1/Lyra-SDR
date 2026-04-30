# Lyra Noise-Toolkit Roadmap

**Status:** RESEARCH / DESIGN — Phase 3.D scoping. No code lands
until this is reviewed.

**Author:** Claude (research deep-dive)
**Date:** 2026-04-30
**Scope:** What's in the noise-toolkit backlog (NR2 / ANF / NB /
captured-noise-profile / neural NR), what each algorithm
actually does, what's pragmatic to ship in Lyra in what order.

---

## 1. Why this document

The Phase 3.B threading work (just merged on `feature/threaded-dsp`)
moved DSP off the Qt main thread, freeing headroom for heavier RX
audio processing. Phase 3.D is the operator-facing payoff: a more
complete noise toolkit. Before writing any code we want a clear
picture of:

- What each algorithm actually does (math + cost), described
  from public DSP literature
- How operators expect to interact with each one (operator-
  facing conventions from existing ham SDR clients)
- Which subset is pragmatic for Lyra given our scope (clean-
  room Python implementation, no third-party DSP code linked or
  ported)
- Sensible ordering — what's worth building first

**A note on clean-room:** Lyra is an independent implementation.
Other ham SDR clients (Thetis, ExpertSDR3) are referenced ONLY
as parallel implementations we can cross-check operator UX
conventions against — they are NOT a source for Lyra code, and
no portion of their source has been read with the intent to
port, adapt, or translate. Algorithm descriptions in this doc
come from public DSP literature (Ephraim & Malah 1984/85,
Martin 2001, Widrow & Hoff LMS theory, etc.) — the same
foundational papers any independent SDR DSP implementation
draws from. The Lyra implementations will be written from those
papers, not from anyone else's code.

Source material consulted for THIS DOC (algorithms only):

- Public papers on spectral subtraction, MMSE-LSA noise
  estimation, LMS adaptive filtering, IQ-domain impulse
  blanking, and minimum-statistics noise tracking.
- Audacity user documentation for the captured-noise-profile
  workflow operators already know.
- Lyra's own existing `lyra/dsp/nr.py` (classical spectral
  subtraction, shipped 2026-04-23 in v0.0.5) — the foundation
  we extend.

---

## 2. What Lyra has today (baseline, v0.0.5)

| Feature | Implementation | Where |
|---|---|---|
| **NR1 — classical spectral subtraction** | STFT, 256-pt Hanning, 50% overlap. Magnitude-domain subtraction with adaptive noise floor (VAD-gated update). Three profiles: Light / Medium / Aggressive. | `lyra/dsp/nr.py` |
| **APF — audio peaking filter** | Single biquad peak filter centered on operator's CW pitch. Lifts narrow CW tone above the audio noise floor without the ringing of a brick-wall narrow filter. | `lyra/dsp/apf.py` |
| **Multi-notch (manual)** | Operator-placed narrow notches, draggable on panadapter, per-notch Hz width / depth. Good for known carriers, broadcast splatter. | Channel-level, `lyra/dsp/channel.py` |
| **BIN — pseudo-binaural** | Hilbert phase split for headphone widening. Not a noise tool but in the listening-tools cluster. | `lyra/dsp/binaural.py` |

Worker thread (Phase 3.B, on `feature/threaded-dsp`) means new
processors run off the Qt main thread for free — anything we add
here gets the threading benefit automatically.

What's **missing** vs full ham-SDR-client parity:

- Captured-noise-profile NR (operator records noise; freeze the
  profile)
- NR2 (MMSE-LSA / minimum-statistics — the "good" non-neural NR)
- ANF (LMS adaptive notch — automatic carrier killer)
- NB (impulse blanker — IQ domain, kills ignition / lightning crashes)
- Neural NR (RNNoise / DeepFilterNet — already stubbed in UI)

---

## 3. Per-feature deep dive

### 3.1 — Captured noise profile (Audacity model)

**Operator concept:** "I'm hearing band noise + my signal. Let me
record 1–2 seconds of just-the-noise (during a transmission gap,
or by tuning to an unused frequency in the band), and use that
captured spectrum as my fixed reference. Then NR subtracts THAT
spectrum from the actual signal — much more accurate than auto-
estimating from the live signal."

**Audacity workflow** (the model operators already know):
1. Select ~0.5–3 sec of noise-only audio
2. Effect → Noise Reduction → "Get Noise Profile"
3. Audacity FFTs the selection, averages the magnitude per bin,
   stores the profile
4. Re-select the full clip → Effect → Noise Reduction → Apply
5. Audacity uses that locked profile for spectral subtraction on
   the whole clip

**Why this is the right "next step" for Lyra**: it's a *small
extension* of the existing `SpectralSubtractionNR` class. Operator
gets a much better noise model than the live VAD-gated estimator,
without bringing in a new DSP algorithm. Differentiator vs other
ham SDR clients — they generally don't expose this Audacity-style
manual capture.

**Operator decisions locked-in 2026-04-30** (these drive the
implementation; if any change, this section + linked code change
together):

| Decision | Locked value |
|---|---|
| Capture duration range | 1.0 / 5.0 sec (default 2.0) |
| Smart-guard | ON by default — refuse-with-warn if signal detected during capture |
| Storage format | One JSON file per profile |
| Storage location | Default `%APPDATA%\Lyra\noise_profiles\` (OS-standard user-data) with **operator-set custom path** option |
| First-cut scope | Capture + use + persistence + naming + management dialog |
| Per-band auto-select | Deferred (polish-pass after base ships) |
| Blend-update on re-capture | Deferred (polish-pass after base ships) |
| Settings location | New **Noise** tab in Settings (separate from DSP) — see §3.1.5 below |

**Day-by-day plan:**

- **Day 1** — `SpectralSubtractionNR` capture state machine,
  smart-guard, progress signal. Self-contained, no UI yet.
- **Day 2** — JSON-per-profile persistence layer
  (`lyra/dsp/noise_profile_store.py` — new module). Atomic
  writes (`*.tmp` + `os.replace`). Default + custom storage
  path.
- **Day 2.5** — Radio integration: `begin_noise_capture()`
  proxy, signal wiring, profile-list signal, settings load on
  startup.
- **Day 3** — UI: Capture button on DSP+Audio panel,
  Settings → Noise tab (just the Captured Profile section),
  Manage Profiles dialog.
- **Day 3.5** — Polish: tooltips, help docs, mode/band
  metadata badge on the panel, age-color rules.

**§3.1.1 — `SpectralSubtractionNR` extension**

New state added to the existing class:

```python
self._captured_noise_mag: Optional[np.ndarray] = None
self._capture_state: str = "idle"   # idle | capturing | ready
self._capture_frames_remaining: int = 0
self._capture_accum: np.ndarray | None = None
self._capture_frame_count_target: int = 0   # for variance/guard math
self._capture_per_frame_powers: list[float] = []  # smart-guard data
```

New public API:

```python
def begin_noise_capture(self, seconds: float = 2.0) -> None: ...
def cancel_noise_capture(self) -> None: ...
def has_captured_profile(self) -> bool: ...
def captured_profile_array(self) -> np.ndarray | None: ...
def load_captured_profile(self, mag: np.ndarray) -> None: ...
def clear_captured_profile(self) -> None: ...
def capture_progress(self) -> tuple[str, float]: ...
   # returns (state, fraction_complete) for UI progress bar
def smart_guard_verdict(self) -> str: ...
   # returns "clean" | "suspect" | "n/a"; called after capture
   # to decide whether to surface a warning
```

**§3.1.2 — Smart-guard**

After capture finishes, before storing the profile, examine the
per-frame power list collected during the capture window:
- Compute mean + standard deviation of per-frame total power
- If `std / mean > GUARD_VARIANCE_THRESHOLD` (~0.5), mark
  verdict as `"suspect"` — high variance suggests a signal was
  riding through the capture.
- Operator sees a Save dialog with the warning surfaced; they
  can save anyway, re-capture, or discard.

The threshold is conservative — quiet band noise has low
frame-to-frame power variance; CW keying or SSB syllables have
much higher variance.

**§3.1.3 — JSON persistence (Day 2)**

New module `lyra/dsp/noise_profile_store.py`:

```python
def list_profiles(folder: Path) -> list[ProfileMeta]: ...
def load_profile(folder: Path, name: str) -> NoiseProfile: ...
def save_profile(folder: Path, profile: NoiseProfile) -> None: ...
def delete_profile(folder: Path, name: str) -> None: ...
def rename_profile(folder: Path, old: str, new: str) -> None: ...
def export_profile(src: Path, name: str, dst: Path) -> None: ...
def import_profile(src: Path, dst_folder: Path) -> str: ...
```

Each profile is a single JSON file in the storage folder:

```json
{
  "schema_version": 1,
  "name": "Powerline 80m",
  "captured_at_iso": "2026-04-30T14:22:13Z",
  "freq_hz": 3825000,
  "mode": "LSB",
  "duration_sec": 2.0,
  "fft_size": 256,
  "lyra_version": "0.0.6",
  "magnitudes": [/* 129 floats — fft_size//2 + 1 */]
}
```

Filename is the sanitized profile name with `.json` suffix.
Atomic writes via `tempfile + os.replace`. On load, schema
mismatch (different `fft_size`) marks the profile incompatible
in the manager UI rather than silently failing.

**§3.1.4 — Storage path resolution**

```python
def get_profile_folder(qsettings) -> Path:
    custom = qsettings.value("noise/profile_folder", "", type=str)
    if custom and Path(custom).is_dir():
        return Path(custom)
    return _default_user_data_dir() / "noise_profiles"
```

`_default_user_data_dir()` resolves per-OS:
- Windows: `%APPDATA%\Lyra`
- macOS: `~/Library/Application Support/Lyra`
- Linux: `~/.local/share/Lyra`

QSettings key: `noise/profile_folder`. Empty/invalid falls back
to default.

**§3.1.5 — UI split (locked)**

Operator-facing controls split between two locations:

**DSP+Audio panel (runtime control surface, existing):**
- NR enable + profile combo (existing — `Light` / `Medium` /
  `Aggressive` plus new `Captured` entry)
- New **Capture Noise Profile** button (greyed unless stream
  is live + non-transmit)
- Right-click on NR cluster → menu with:
  - "Manage Profiles…" → opens management dialog
  - "Open Noise settings…" → opens Settings on the Noise tab
- When a captured profile is the active NR profile, the panel
  shows a small inline label: `Captured: "Powerline 80m" (3 days, 80m LSB)`
  with amber/red coloring on the age per the rules in §3.1.6.

**Settings → Noise tab (new, this commit creates it):**
- Capture duration slider (1.0 – 5.0 sec, default 2.0)
- Smart-guard toggle (on by default)
- Storage location selector (Default / Custom + Browse)
- Profile age-warning thresholds (amber after N hours, red after
  N days — defaults 24 h / 7 days)
- "Open profile manager…" button
- "Open profiles folder…" button (OS file-explorer launch)
- Greyed-out reserve sections for NB / ANF / NR2 are NOT shown
  on day one — they appear when those features ship (3.2 / 3.3
  / 3.4).

**Why a new tab vs squeezing into DSP:** the DSP tab already
holds AGC + Threading + EQ placeholder; adding noise-toolkit
tuning would push it past comfortable readability. The Noise
tab also gives a natural home for NB / ANF / NR2 settings as
those features land — they all join the same tab.

**§3.1.6 — Mode/band/age metadata UI rules**

When the active NR profile is `"captured"`, the DSP+Audio
panel shows an inline status label:

```
Captured: "Powerline 80m"  (3 days old, 80m LSB) ⚠
```

- Age coloring (operator-tunable in the Noise settings tab):
  - <amber threshold (default 24 h): grey, no badge
  - amber threshold to red threshold: amber color
  - >red threshold (default 7 days): red color
- Mode/band mismatch warning ⚠: shown if active mode or
  active band differs from the captured `mode`/derived band
  of the active profile. Hover tooltip explains.
- No auto-disable. Operator decides whether to use a profile
  with stale age or wrong band — Lyra surfaces the info but
  doesn't prevent.

**Effort**: ~2.5–3 days for the captured-profile feature with
the Settings tab properly set up. Touches `lyra/dsp/nr.py`
(extend), new `lyra/dsp/noise_profile_store.py` (persistence),
`lyra/radio.py` (integration), `lyra/ui/panels.py` (DSP+Audio
panel additions), `lyra/ui/settings_dialog.py` (new Noise tab),
new `lyra/ui/noise_profile_manager.py` (manager dialog), and
help docs.

**Why this is "doable now"**: NR1 is already shipping and
operator-tested. Adding a captured-profile mode is mechanical;
the math is identical to what NR1 does today, just with a
different source for `_noise_mag`. The persistence + UI work
is straightforward Qt + JSON. No new algorithms, no third-
party code, no change to NR1's existing single-thread or
worker-mode behavior for operators who don't opt in.

---

### 3.2 — NR2 (MMSE-LSA, Ephraim-Malah)

**Algorithm**: MMSE-LSA (Minimum Mean-Squared Error Log-Spectral
Amplitude) noise reduction, Ephraim & Malah 1984/1985. The
state-of-the-art for non-neural speech noise reduction.

**Why it's better than NR1 (spectral subtraction)**:
- NR1's per-bin gain function `G(k) = max(1 - α·N̂(k)/Y(k), β)`
  is crude — the gain depends only on the current frame's
  estimated SNR. When SNR is moderate this introduces *musical
  noise* (random bins flickering above the floor → "chirpy" or
  "underwater" artifact).
- NR2 uses a soft-decision-directed gain `G(k) = f(ξ(k), γ(k))`
  where:
  - `γ(k)` = a-posteriori SNR (current frame)
  - `ξ(k)` = a-priori SNR (smoothed across frames — the key idea)
- The smoothing of `ξ` between frames eliminates almost all
  musical noise. The resulting audio sounds "naturally quiet"
  rather than "noise-suppressed".

**Reference**: Ephraim & Malah, "Speech Enhancement Using a
Minimum Mean-Square Error Log-Spectral Amplitude Estimator,"
IEEE Trans. ASSP, 1985. Plus Martin's "Noise Power Spectral
Density Estimation Based on Optimal Smoothing and Minimum
Statistics," IEEE Trans. SAP, 2001, for the noise tracker.

**Lyra implementation cost**: Heaviest item on the list.
- Core MMSE-LSA gain function (with smoothed-`ξ` + decision-
  directed update): ~50 lines NumPy/SciPy.
- Noise estimator: minimum-statistics is ~200 lines of careful
  bookkeeping. Or — pragmatic shortcut — start with
  continuous-spectral-minimum tracking (~30 lines) and upgrade
  to full minimum-statistics later. Real-world quality
  difference is small on stationary band noise; minimum-
  statistics shines on non-stationary noise.
- Setup: pre-compute the EMNR gain table `G(γ, ξ)` at init
  (NumPy meshgrid → store), vectorized lookup at runtime.

**Effort**: ~3–5 days for a competent first cut written from
the Ephraim-Malah and Martin papers. **Real** musical-noise-
free quality requires careful tuning + listening tests across
multiple bands and operators.

**Where this fits in Lyra's NR profile combo**:
- Light (NR1)
- Medium (NR1)
- Aggressive (NR1)
- **High Quality (NR2)** ← new
- Captured (NR1 with locked profile) ← also new (3.1)
- Neural (placeholder, deferred)

---

### 3.3 — ANF (Automatic Notch Filter, LMS adaptive)

**Algorithm**: LMS (Least Mean Squares) adaptive predictor.
The filter learns to predict the current sample from delayed
samples; tonal interference (carriers, hetorodyne whistles,
RTTY spurs) is highly predictable so it gets nulled in the
prediction; broadband audio is unpredictable so it survives in
the residual.

The "automatic notch" output choice is the *residual error*
`e[n] = x[n] - ŷ[n]` — useful audio survives, tones are killed.
The companion algorithm "ANR" (automatic noise reduction) uses
the *prediction* `ŷ[n]` instead — tonal speech parts are
enhanced, broadband noise is rejected. Same predictor, opposite
output. ANF is the more commonly-used variant.

**Reference**: standard LMS adaptive-filter theory (Widrow &
Hoff). The "leakage" / "leaky LMS" extension that prevents
weight drift on stationary inputs is also classical (Gitlin,
Mazo, Taylor 1973). Adaptive-leakage variants where the
leakage parameter itself adapts based on signal energy are a
common refinement in production implementations.

**Lyra implementation**: Drop in cleanly as an audio-domain
processor in the rx_channel, *after* notch filters and *before*
NR. New file `lyra/dsp/anf.py`. Knobs:
- Taps (typical default 64)
- Delay (typical 10 — gives the predictor a recent-but-not-zero
  history)
- `μ` (gain — typical 1e-4)
- `γ` (leakage base — typical 0.10)

**Effort**: ~1 day for a clean Python implementation written
from the LMS literature + tests. Listening test on a real band
with a hetorodyne is the only reliable verification.

**UI**: New ANF toggle in the DSP+Audio panel cluster (next to
APF). Right-click → presets (gentle / standard / aggressive).
Operator-noticeable benefit: tunable hetorodyne killer that
doesn't notch out genuine SSB content like a manual-notch can.

---

### 3.4 — NB (Noise Blanker, IQ-domain impulse blanker)

**Algorithm**: Detect-then-replace impulse blanking, IQ-domain
(pre-demod). For each IQ sample, compare its instantaneous
magnitude against a smoothed reference; if it exceeds the
reference by a threshold, mark it as an impulse. Replace
impulse samples with predicted values (typically a backward
exponential extrapolation from clean samples on either side),
with a smoothed transition (cosine slew) to avoid clicks at the
edges.

**Why IQ-domain (not audio)**: An impulse in the antenna feedline
hits the bandpass filter and *spreads* across the audio passband.
By the time it reaches the audio chain, you can't easily tell
which audio samples are the impulse and which are signal. In the
IQ domain, before bandpass filtering, the impulse is still a
narrow time-domain spike — easy to detect and replace.

**Reference**: classical IQ-domain impulse blanking is
well-documented in HF receiver design literature dating back to
analog days; the digital version is a straightforward
adaptation. Operator-facing parameters (threshold, advance/hang
slew time, max consecutive blanked samples) are conventional
across SDR client implementations.

**Lyra implementation**: New module `lyra/dsp/nb.py`. Slot into
Radio's DSP chain *before* `_rx_channel.process(samples)` — i.e.
between the IQ-arrival path and the channel pipeline. Affects
all downstream (NR, demod, audio) because the impulse never gets
to spread.

Per-sample work:
- Maintain exponentially-smoothed background magnitude `bg`
- Compute current sample magnitude `m = |x|²`
- If `m > threshold·bg`: mark sample for blanking, replace with
  `bg`-derived prediction
- Apply slewed transitions (cosine taper) at edges so the
  replacement doesn't create click artifacts of its own
- Cap consecutive blanked samples (a continuous strong signal
  shouldn't be blanked away)

**Effort**: ~2 days. Detection threshold is the operator-facing
knob; everything else has good defaults from HF receiver
practice.

**UI**: New NB button in the DSP+Audio cluster (alongside NR /
APF). Threshold slider + presets (Off / Light / Medium /
Aggressive). Visible benefit: car ignition noise, switching
power supplies, lightning crashes go from "loud popping" to
"barely audible click".

---

### 3.5 — Neural NR (RNNoise / DeepFilterNet)

**Already detection-stubbed in Lyra**: `Radio.neural_nr_available()`
tries to import `rnnoise_wrapper` and `deepfilternet`; UI has a
"Neural" entry in the NR profile right-click menu, greyed out
when neither package is importable.

**Two contenders**:
- **RNNoise** (Jean-Marc Valin / Mozilla): tiny RNN, ~1% CPU,
  designed specifically for speech NR. Best speech-on-noise
  quality per watt. Real-time on any modern PC.
- **DeepFilterNet** (Rust + Python bindings): larger model,
  noticeably better quality than RNNoise; ~5% CPU on a modern
  laptop. Real-time.

**Why deferred**: not the speech-on-noise quality is the issue —
it's the dependency / packaging story. Both require either a
compiled binary (RNNoise via `rnnoise-wrapper`) or a Rust
toolchain (DeepFilterNet). Lyra's installer story is currently
"`pip install` Python deps, run `python -m lyra.ui.app`" —
adding a binary blob or compiled wheel raises the bar.

**Recommended sequencing**: ship 3.1 → 3.4 (above) first, get
operator field reports on the captured-profile and EMNR options,
then evaluate whether the neural path is still worth the
packaging complexity. Spectral-subtraction with a captured
profile is *very close* to neural quality on stationary noise —
the neural advantage is mostly on non-stationary speech-vs-
speech scenarios, less common in HF radio.

---

## 4. Recommended Lyra build order

| # | Feature | Effort | Operator value | Build complexity |
|---|---|---|---|---|
| **1** | **Captured noise profile** (NR1 + locked profile) | 1 day | High — operator-controllable noise model, big quality win on stationary band noise | Trivial — extends existing `SpectralSubtractionNR` |
| **2** | **NB — IQ impulse blanker** | 2 days | High — eliminates ignition / lightning impulse noise (very visible to operator) | Moderate — new processor, slot into Radio before channel |
| **3** | **ANF — LMS adaptive notch** | 1 day | Medium-high — automatic hetorodyne killer | Trivial — clean ~30-line LMS, drop into channel after manual notch |
| **4** | **NR2 — MMSE-LSA / Ephraim-Malah** | 3–5 days | High — best-in-class non-neural NR; eliminates musical-noise artifact of NR1 | Moderate — careful per-bin state, gain table, noise estimator |
| **5** | **Neural NR** (RNNoise or DeepFilterNet) | 2–3 days code + packaging | Medium-high (incremental over NR2) | Hard — packaging / dependency story |

**My recommendation for the next release window:**

Land **#1 (captured profile)** in v0.0.6 alongside the threading
work. Small, low-risk, high operator value, ships *now* with no
new algorithms. The "Audacity-style" workflow you mentioned is
exactly this.

Then in v0.0.7, do **#2 (NB)** and **#3 (ANF)** as a paired
release — they're both small, both target specific known noise
sources operators recognize, both drop in cleanly. Together they
fill the biggest functional gaps in Lyra's noise toolkit.

**#4 (NR2)** is the headline feature for v0.0.8 or v0.0.9 — it's
a meaningful new DSP module that genuinely changes the listening
experience on a noisy band. Worth taking the time to do well.

**#5 (Neural)** stays deferred until packaging story is clearer.

---

## 5. Open questions / operator input

- **Profile storage** — should captured profiles persist across
  Lyra restarts? (Per-band? Per-day? Just one slot?)
- **Capture trigger** — automatic during transmission gaps? Or
  always operator-button-driven?
- **NR2 default profile** — once NR2 ships, should the default
  NR profile change from "Medium (NR1)" to "High Quality (NR2)"?
  Or keep NR1 default for backwards consistency and let
  operators opt in?
- **NB position in chain** — pre-decimation (raw HL2 IQ rate, max
  impulse spike-width preserved)? Or post-decimation in the
  channel (saves CPU but slightly less impulse-aware)? Common
  practice is pre-decimation.
- **ANF position** — before or after NR? Default proposal: after
  notches, before NR — so notches handle known carriers, ANF
  handles unknown tones, NR handles whatever's left.

---

## 6. Reference: Lyra's RX chain (current + proposed)

**Lyra's current order** (post-Phase-3.B):
```
HPSDR rx → DspWorker.process_block:
  → channel.process(iq):
       decim → notch → demod → NR1 → APF
  → AGC + AF Gain + Volume
  → BIN
  → audio_sink.write
```

**Phase 3.D target order** (proposed):
```
HPSDR rx → DspWorker.process_block:
  → NB (impulse blank, IQ domain)        ← NEW (3.4)
  → channel.process(iq):
       decim → notch → demod
       → ANF (tones)                     ← NEW (3.3)
       → NR (NR1 / Captured / NR2)       ← extended (3.1, 3.2)
       → APF
  → AGC + AF Gain + Volume
  → BIN
  → audio_sink.write
```

**Rationale for the order:**

- **NB first, IQ-domain**: impulses spread through bandpass
  filters; kill them before they spread. Pre-decimation keeps
  the impulse spike at maximum time-resolution.
- **Manual notches before ANF**: notches are deterministic
  (operator placed them on known carriers); ANF handles
  unknown / drifting tones that survived. This lets ANF's LMS
  predictor focus on residuals rather than fighting against
  notches that already work.
- **ANF before broadband NR**: LMS notching produces a clean
  residual; broadband NR estimators (NR1's noise floor, NR2's
  Martin minimum) work better on a tone-free input.
- **AGC last**: the cleanest signal sets the cleanest
  envelope. AGC seeing pre-NR audio would chase noise floor
  bumps that NR is about to remove.

This matches conventional ham SDR client chain ordering and is
consistent with how the algorithms are described in the public
DSP literature.

---

## 7. Cross-references

- Phase 3.B (threading) — `docs/architecture/threading.md`
- WDSP integration roadmap — `docs/architecture/wdsp_integration.md`
- Current NR1 implementation — `lyra/dsp/nr.py`
- NR help doc — `docs/help/nr.md`
- Backlog (full priority list) — `docs/backlog.md`

This doc is the source of truth for Phase 3.D scoping decisions.
Operator review welcome before any sub-task lands; once approved,
each item above becomes its own commit on a feature branch off
`main`, same model as `feature/threaded-dsp`.

All Lyra implementations of these algorithms will be written
clean-room from the public DSP literature cited above. No code
from any other SDR client has been read with the intent to port,
adapt, or translate, and that constraint applies to all Phase
3.D work going forward.
