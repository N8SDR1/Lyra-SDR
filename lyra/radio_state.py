"""Radio dispatch-state contract (v0.1 Phase 0).

This module is the single source of truth for Lyra's runtime
**state product** — the small set of orthogonal axes that govern
how the protocol layer routes per-DDC samples, how the panadapter
chooses its spectrum source, and how the captured-profile pre-pass
decides whether to bypass during PS+TX.

Authoritative spec:
``docs/architecture/v0.1_rx2_consensus_plan.md`` §4.2.x (added
Round 3 2026-05-11 per R3-3; full read+write surface lands in
Phase 0 per R5-3 Option A).

Why this lives at the ``lyra/`` package root (not in
``lyra/protocol/`` or ``lyra/dsp/``):

The state is consumed across all three layers:

* **Protocol** (``HL2Stream._rx_loop`` reads
  ``radio.snapshot_dispatch_state()`` once per UDP datagram to
  drive ``radio.protocol.ddc_map(state)`` family-specific routing)
* **DSP** (``Radio._do_demod_wdsp`` reads the snapshot per WDSP
  block for the captured-profile bypass edge-detector)
* **UI** (panadapter source-switch hangs off
  ``dispatch_state_changed`` Qt signal in v0.2+; settings UI may
  read snapshot for read-out only)

Placing ``DispatchState`` at the package root means none of those
layers imports another's module just for the state shape.

Threading contract (§4.2.x "Threading model summary"):

* **Qt main thread** is the sole writer (via ``Radio.set_mox``,
  ``Radio.set_ps_armed``, ``Radio.set_rx2_enabled``,
  ``Radio.set_radio_family``).
* **All other threads** (RX loop, DSP worker) are readers only
  via ``Radio.snapshot_dispatch_state()``.
* The dataclass is ``frozen=True`` — mutation must go through
  ``dataclasses.replace(...)`` which produces a new instance.
* CPython's GIL ensures the reference read of
  ``self._dispatch_state`` is atomic; no ``threading.Lock``
  required.
* Mid-datagram MOX edges are coalesced to the next datagram
  boundary (~1 ms granularity at 192 kHz).  See §4.2.x "Setter
  atomicity note" for rationale.

Phase 0 ships the contract (this module + the surface on
``Radio``).  Phase 1 adds the consumer side:
``radio.protocol.ddc_map(state) -> Dict[int, ConsumerID]`` pure
function + the ``dispatch_ddc_samples(...)`` rewrite in
``stream.py``.  ``ConsumerID`` is exported here in Phase 0 so
Phase 1's ddc_map signature can be drafted without an additional
module import.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RadioFamily(Enum):
    """Hardware-family discriminator for the dispatch routing.

    The ``family`` field on ``DispatchState`` selects which
    per-family ``ddc_map`` implementation Phase 1's
    ``radio.protocol.ddc_map(state)`` dispatches to.  v0.1 Phase 0
    only populates ``HL2`` and ``HL2_PLUS``; ``ANAN_P1_5DDC`` and
    ``ANAN_P2`` are placeholders for v0.4 multi-radio work per
    consensus-plan §6.7 + ``CLAUDE.md`` §6.7 discipline #6.

    Don't add per-family flags here -- per-family deltas live on
    ``RadioCapabilities`` (Phase 0 item 8); this enum is just the
    routing-table selector key.
    """
    HL2 = "hl2"
    HL2_PLUS = "hl2_plus"
    ANAN_P1_5DDC = "anan_p1_5ddc"
    ANAN_P2 = "anan_p2"
    # v0.4 expansion -- Orion / Hermes-II / Brick etc. land here


@dataclass(frozen=True)
class DispatchState:
    """Snapshot of the four axes that drive per-DDC dispatch routing.

    The state product is **orthogonal** -- every (mox, ps_armed,
    rx2_enabled, family) tuple is a legal state, and the dispatch
    table is total over the cross-product.  Adding a fifth axis
    would force a re-derivation of every per-family ddc_map; don't.

    Defaults match "fresh radio, no operator activity":
    * ``mox=False`` -- RX-only
    * ``ps_armed=False`` -- PS dialog has not entered the armed FSM
      state (operator either has no PS hardware mod, or hasn't
      enabled PS for this session)
    * ``rx2_enabled=False`` -- single-RX operation (v0.0.x parity)
    * ``family=HL2`` -- conservative default for the v0.1 / v0.2 /
      v0.3 release line; replaced at discovery time by
      ``Radio.set_radio_family(...)`` once the gateware identifies.

    Mutation pattern: ALWAYS via ``dataclasses.replace(state,
    mox=new)``.  Field-by-field assignment is forbidden by
    ``frozen=True``.  Setters on ``Radio`` enforce this.
    """
    mox: bool = False
    ps_armed: bool = False
    rx2_enabled: bool = False
    family: RadioFamily = RadioFamily.HL2


class ConsumerID(Enum):
    """Where a given DDC's samples are routed, per the dispatch
    table.

    Phase 0 ships the enum; Phase 1's ``radio.protocol.ddc_map(state)
    -> Dict[int, ConsumerID]`` pure function produces an instance of
    ``Dict[int, ConsumerID]`` from a ``DispatchState`` per the
    family-specific table.  Phase 1 also wires the consumer-side
    handlers in ``HL2Stream._rx_loop`` that read this dict and call
    the right downstream method (``Radio.dispatch_rx1``,
    ``Radio.dispatch_rx2``, ``Radio.dispatch_ps_feedback_i``, etc.).

    Example HL2 dispatch table from §4.2.x:

    | State                    | DDC0           | DDC1           | DDC2    | DDC3    |
    |--------------------------|----------------|----------------|---------|---------|
    | ``(mox=False, *)``       | RX_AUDIO_CH0   | RX_AUDIO_CH2   | DISCARD | DISCARD |
    | ``(True, ps_armed=False)`` | RX_AUDIO_CH0 | RX_AUDIO_CH2   | DISCARD | DISCARD |
    | ``(True, True)``         | PS_FEEDBACK_I  | PS_FEEDBACK_Q  | DISCARD | DISCARD |

    The ``RX_AUDIO_CH*`` consumers feed Lyra host channels 0 / 2
    (per CLAUDE.md §6.7 discipline #6 -- host channel ID is not
    DDC index).  Panadapter taps are siblings of the audio
    consumers, not exclusive of them -- the same DDC samples can
    feed both a demod chain AND the panadapter (the dispatch table
    typically picks ONE, and the panadapter source-switch handles
    the v0.2/v0.3 TX-baseband / PS-feedback overlay separately via
    ``SpectrumSourceMixin``).
    """
    RX_AUDIO_CH0 = "rx_audio_ch0"             # host channel 0 demod chain (RX1)
    RX_AUDIO_CH2 = "rx_audio_ch2"             # host channel 2 demod chain (RX2)
    PS_FEEDBACK_I = "ps_feedback_i"           # calcc I input (HL2 DDC0 with cntrl1=4)
    PS_FEEDBACK_Q = "ps_feedback_q"           # calcc Q input (HL2 DDC1 sync to DDC0)
    PANADAPTER_TAP_RX1 = "panadapter_rx1"     # RX1 band → SpectrumSourceMixin
    PANADAPTER_TAP_RX2 = "panadapter_rx2"     # RX2 band → SpectrumSourceMixin
    DISCARD = "discard"                       # gateware-disabled or otherwise unused
