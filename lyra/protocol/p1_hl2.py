"""HL2-specific HPSDR P1 protocol constants + math.

This module is the home for HL2 gateware quirks that don't belong
in the generic protocol layer (per CLAUDE.md §6.7 discipline #5:
"TX hardware quirks live in protocol module, not DSP").  The
``lyra/protocol/stream.py`` wire-emission code stays family-agnostic
where possible; values that vary per radio family (HL2 vs ANAN P1
vs ANAN P2) live here for v0.2 / v0.3 and will be joined by
``p1_anan.py`` + ``p2_anan.py`` siblings when v0.4 multi-radio
support lands.

v0.2 Phase 1 (10/10): created as a placeholder.  Phase 3 UI work
fills in:

* Forward-power calibration constants per HL2 board revision.
  Default starter coefficients for the quadratic fit
  ``fwd_w = cal_a * fwd_pwr_adc**2 + cal_b * fwd_pwr_adc + cal_c``
  (operator self-cal via Settings -> TX -> Calibrate replaces
  these with per-board 3-point measurements -- see consensus plan
  §8.4(a) for the calibration UX spec).

* SWR computation from reflection coefficient:
  ``rho = sqrt(rev_w / max(fwd_w, eps))``
  ``swr = (1 + rho) / max(1 - rho, eps)``

* HL2 TX step-attenuator range bounds (-28..+31 dB) -- duplicates
  the value in ``RadioCapabilities.tx_attenuator_range`` so this
  module can validate operator input independently of the
  capability struct (defense in depth against partial-init order).

The forward-power formula starter values (HL2 community default):

    volts = (fwd_pwr_adc - 6) / 4095 * 3.3
    watts = volts ** 2 / 1.5

This factors back into the quadratic:

    cal_a = (3.3 / 4095) ** 2 / 1.5
    cal_b = -2 * 6 * cal_a
    cal_c = 6 * 6 * cal_a

Phase 3 ships these as default cal coefficients with an
"uncalibrated -- run 3-point self-cal" badge until the operator
runs the calibration workflow.
"""
from __future__ import annotations

# Placeholders -- Phase 3 work fills these in with the real values
# + the per-band cal storage scheme.  Importing the module here
# reserves the file location and gives consumers (Settings UI
# scaffolding, Radio's forward-power signal emitter) a stable
# import path.
