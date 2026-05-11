"""Lyra protocol module.

Phase 0 (v0.1) exposed the multi-radio capability surface
(``lyra.protocol.capabilities``) and the dispatch-state contract
types (``lyra.radio_state``).

Phase 1 (v0.1) adds ``ddc_map(state)`` — the pure function that
maps a ``DispatchState`` snapshot to a per-DDC consumer routing
table.  Per CLAUDE.md §6.7 discipline #6 the table is
**family-specific** AND **state-product-dependent**, so this
function dispatches on ``state.family`` and uses
``state.mox`` + ``state.ps_armed`` to pick the table row.

Authoritative spec:
* ``docs/architecture/v0.1_rx2_consensus_plan.md`` §4.2.x
  "Dispatch state contract" — defines the dataclass + enum and
  the example HL2 dispatch table.
* ``CLAUDE.md`` §6.7 discipline #6 — the "DDC mapping is
  family-specific AND state-product-dependent" rule that this
  function operationalizes.
* ``CLAUDE.md`` §3.8 "PS feedback DDC routing" — corrected
  Round 1 entry establishing that HL2 PS feedback lives on
  DDC0+DDC1 (cntrl1=4 routing), NOT DDC2+DDC3.

Phase 1 implements HL2 / HL2_PLUS only.  ANAN_P1_5DDC and
ANAN_P2 raise ``NotImplementedError`` -- v0.4 multi-radio work
fills them in.  Sibling protocol modules at that point will own
their per-family ddc_map (``lyra/protocol/anan.py::anan_ddc_map``,
etc.) and this module's ``ddc_map`` will dispatch into them.
"""
from __future__ import annotations

from typing import Dict

from lyra.radio_state import ConsumerID, DispatchState, RadioFamily


# ──────────────────────────────────────────────────────────────────────
# HL2 dispatch tables (per CLAUDE.md §6.7 discipline #6).
#
# These are module-level dict literals so ``ddc_map`` is a pure
# table-lookup function -- no per-call allocation, no branches
# beyond the state-product key derivation.  Each table is a
# {ddc_index: ConsumerID} mapping; the key is the wire-protocol
# DDC slot (0..3), the value is the consumer the dispatcher should
# route the per-DDC samples to.
#
# Tables are total over their declared key set (all four DDCs
# always have an assignment).  ``DISCARD`` is used for slots whose
# samples have no live consumer in the current state — on HL2 that
# is DDC2/DDC3 in every state, because HL2 gateware leaves those
# slots zero-filled regardless of PS state (CLAUDE.md §3.8
# corrected entry: HL2 PS feedback uses DDC0/DDC1 via cntrl1=4,
# NOT DDC2/DDC3 as older docs claimed).

_HL2_DDC_MAP_RX_ONLY: Dict[int, ConsumerID] = {
    0: ConsumerID.RX_AUDIO_CH0,   # RX1 audio chain
    1: ConsumerID.RX_AUDIO_CH2,   # RX2 audio chain (Phase 1 inert; Phase 2 wires audio)
    2: ConsumerID.DISCARD,         # gateware-disabled on HL2
    3: ConsumerID.DISCARD,         # gateware-disabled on HL2
}

_HL2_DDC_MAP_MOX_NO_PS: Dict[int, ConsumerID] = {
    # Same as RX-only.  Operator's MuteRX*OnVFOBTX gate operates at
    # the AAmixer level per audio_architecture.md §2.4, NOT at the
    # protocol-dispatch level — the protocol layer still delivers
    # RX-band content; the mixer decides whether it's audible.
    0: ConsumerID.RX_AUDIO_CH0,
    1: ConsumerID.RX_AUDIO_CH2,
    2: ConsumerID.DISCARD,
    3: ConsumerID.DISCARD,
}

_HL2_DDC_MAP_MOX_PS_ARMED: Dict[int, ConsumerID] = {
    # CLAUDE.md §3.8 PS feedback DDC routing (corrected Round 1):
    # HL2 gateware re-routes the PA-coupler ADC to DDC0 via cntrl1=4,
    # and DDC1 is sync-paired to DDC0 at TX freq.  The captured-
    # profile pre-pass (§14.6) must ALSO bypass on this state edge
    # because DDC0 is no longer carrying RX1-band content.
    0: ConsumerID.PS_FEEDBACK_I,
    1: ConsumerID.PS_FEEDBACK_Q,
    2: ConsumerID.DISCARD,        # gateware-disabled on HL2 PS+TX
    3: ConsumerID.DISCARD,        # gateware-disabled on HL2 PS+TX
}


def ddc_map(state: DispatchState) -> Dict[int, ConsumerID]:
    """Map a dispatch state snapshot to per-DDC consumer routing.

    **Pure function** — no side effects, no globals beyond the
    module-level table literals, no I/O.  Same input always
    produces same output.  This invariant is relied on by:

    * Unit tests (consensus plan §4.4 verification step 7) that
      programmatically toggle MOX / PS-armed and assert the
      returned dict matches the expected per-family table cell.
    * Thread-safety reasoning: the dispatcher in ``stream.py``
      can call this from the RX loop thread without locking,
      because the function depends only on its arguments.
    * The captured-profile bypass edge-detector in
      ``Radio._do_demod_wdsp`` (§4.2.x captured-profile bypass
      call site), which compares the result across consecutive
      snapshots to detect rising / falling MOX+PS edges.

    Phase 1 (v0.1) implements HL2 / HL2_PLUS only.  Other
    ``RadioFamily`` values raise ``NotImplementedError`` --
    v0.4 multi-radio adds ANAN P1 / P2 tables (CLAUDE.md §6.7
    discipline #6).

    HL2 / HL2_PLUS dispatch table:

    +---------------------------------+--------------+--------------+---------+---------+
    | State                           | DDC0         | DDC1         | DDC2    | DDC3    |
    +=================================+==============+==============+=========+=========+
    | ``(mox=False, *)`` RX-only      | RX_AUDIO_CH0 | RX_AUDIO_CH2 | DISCARD | DISCARD |
    +---------------------------------+--------------+--------------+---------+---------+
    | ``(True, ps_armed=False)``      | RX_AUDIO_CH0 | RX_AUDIO_CH2 | DISCARD | DISCARD |
    +---------------------------------+--------------+--------------+---------+---------+
    | ``(True, True)`` MOX+PS armed   | PS_FEEDBACK_I| PS_FEEDBACK_Q| DISCARD | DISCARD |
    +---------------------------------+--------------+--------------+---------+---------+

    Note ``rx2_enabled`` does NOT affect the wire-protocol routing:
    the gateware always delivers DDC1 samples regardless of whether
    Lyra is consuming them as RX2 audio.  When ``rx2_enabled=False``
    the consumer registered at ``RX_AUDIO_CH2`` may discard
    internally (Phase 1 design: register a no-op consumer until
    Phase 2's RX2 audio chain lands).  The dispatch table itself
    is unchanged by that bit.

    Args:
        state: A ``DispatchState`` snapshot.  Reads ``.mox``,
            ``.ps_armed``, and ``.family``; ignores ``.rx2_enabled``
            (intentional — see note above).

    Returns:
        Dict mapping wire-protocol DDC index (0..3) to the
        ``ConsumerID`` enum value that should receive that DDC's
        samples for this state.

    Raises:
        NotImplementedError: If ``state.family`` is not HL2 or
            HL2_PLUS.  Phase 1 scope; v0.4 fills in ANAN tables.
    """
    if state.family in (RadioFamily.HL2, RadioFamily.HL2_PLUS):
        if state.mox and state.ps_armed:
            return _HL2_DDC_MAP_MOX_PS_ARMED
        if state.mox:
            return _HL2_DDC_MAP_MOX_NO_PS
        return _HL2_DDC_MAP_RX_ONLY
    raise NotImplementedError(
        f"ddc_map for family {state.family.value!r} not implemented "
        f"in v0.1 Phase 1; v0.4 multi-radio refactor adds ANAN P1 / "
        f"P2 dispatch tables.  See CLAUDE.md §6.7 discipline #6."
    )


__all__ = ["ddc_map"]
