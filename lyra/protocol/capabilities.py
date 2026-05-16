"""Radio-class capability struct.

This module is the single source of truth for **per-radio-family
hardware capabilities** -- the orthogonal axes UI code branches on
instead of doing ``isinstance(radio.protocol, HL2)`` checks.

Authoritative spec:
* ``docs/architecture/v0.1_rx2_consensus_plan.md`` §3.1.x item 8
  (Phase 0 done-definition).
* ``CLAUDE.md`` §13.4 (HL2 audio capability fields).
* ``CLAUDE.md`` §6.7 discipline #6 (DDC mapping is family-specific
  AND state-product-dependent -- the per-family ``ddc_map`` lives
  in ``lyra/protocol/<family>.py`` next to the capability instance).

Why this exists at all
======================

v0.4 will add ANAN family support (G2 / G2-1K / 7000DLE / 8000) per
operator decision 2026-05-03.  v0.1 / v0.2 / v0.3 stay HL2-only by
scope, but the **hardware-abstraction discipline** in CLAUDE.md §6.7
says: write capability-driven UI now so the v0.4 retrofit is one new
module (``hl2_plus_capabilities``, ``anan_g2_capabilities``, etc.),
not a months-long refactor of every UI call site.

The audit gate ``! rg "isinstance.*HL2" lyra/ui/`` (Phase 0 item 9)
enforces this: any time a UI file reaches for the radio's protocol
class name to make a decision, that's a smell and the CI grep fails
the commit.  Use the capability struct instead.

Phase 0 scope
=============

Phase 0 defines the dataclass surface, populates ``HL2_CAPABILITIES``
with verified-against-CLAUDE.md-§3 values, and exposes
``Radio.capabilities`` returning the HL2 instance unconditionally
(no discovery-time selection yet -- v0.4 wires that).

Phase 0 callers MAY read only ``nddc`` + ``default_audio_path`` +
``has_onboard_codec`` per consensus plan §3.1.x item 8.  The
remaining fields are populated for completeness so v0.2 / v0.3 /
v0.4 callers don't have to extend the struct as they land -- but
they're explicitly NOT consumed by Phase 0 code.

Future families (v0.4):
``HL2_PLUS_CAPABILITIES``, ``ANAN_G2_CAPABILITIES``,
``ANAN_7000DLE_CAPABILITIES``, ``ANAN_8000_CAPABILITIES``, etc.
Each gets its own populated instance in a sibling module under
``lyra/protocol/``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AudioPath(Enum):
    """Operator's default audio-output path.

    Per CLAUDE.md §13.1 there are two paths:

    * ``HL2_CODEC`` -- audio leaves Lyra via EP2 LRIQ bytes, plays
      through the HL2's onboard AK4951 (single-crystal, zero clock
      drift, no resampler needed).  Default for HL2 / HL2+ hardware.
    * ``PC_SOUND`` -- audio plays through the host PC's sound card
      via WASAPI/PortAudio.  Default for ANAN family (no onboard
      codec); operator-selectable on HL2 too.

    Operator can override on either family in Settings -> Audio.
    The capability struct's ``default_audio_path`` is what fresh
    installs (no override yet persisted) start with.
    """
    HL2_CODEC = "hl2_codec"
    PC_SOUND = "pc_sound"


@dataclass(frozen=True)
class RadioCapabilities:
    """Per-radio-family hardware capability surface.

    Frozen so UI code can pass references around without worrying
    about hidden mutation -- if the capabilities ever need to
    change at runtime (e.g., an operator-toggled "treat HL2+ as
    HL2 classic" setting), the right pattern is to swap the
    struct via ``Radio.set_capabilities(new_struct)``, not to
    mutate fields on the existing one.

    Field documentation (Phase 0 baseline -- v0.4 may add more):

    * ``family_name``: human-readable identifier ("Hermes Lite 2",
      "ANAN G2", etc.).  Surfaced in About dialog and crash
      reports.  Don't make decisions on this -- use the other
      fields.

    * ``nddc``: advertised DDC count on the wire.  HL2 = 4 (even
      though silicon has 2; gateware exposes 4 logical DDCs).
      ANAN G2 = 4, 7000DLE = 7, etc.  Phase 0 callers read this;
      protocol layer's EP6 parser reads this for sample-set stride.

    * ``has_onboard_codec``: True if the radio has an integrated
      audio codec (HL2's AK4951).  False for ANAN family.  Phase 0
      callers read this to decide whether the HL2-jack audio
      output is offered in Settings.

    * ``default_audio_path``: which audio path a fresh install
      defaults to.  HL2 = ``HL2_CODEC`` (the always-works
      single-crystal path).  ANAN = ``PC_SOUND``.  Phase 0
      callers read this on first launch + when the operator
      resets Audio settings.

    * ``puresignal_requires_mod``: True if the operator must
      install a hardware mod (RF coupler from PA to DDC2/3 input)
      for PS feedback.  HL2 = True (operator self-attestation per
      §6.5).  ANAN G2 = False (mod built into stock gateware).
      v0.3 PSDialog reads this to decide whether to surface the
      "I have the mod installed" checkbox.

    * ``tx_attenuator_range``: TX drive's hardware-side range as
      ``(min_db, max_db)``.  HL2 = ``(-28, 31)`` (negative values
      are GAIN, not attenuation -- HL2 quirk per CLAUDE.md §3.8).
      ANAN = ``(0, 31)`` (standard).  v0.2 TX drive slider reads
      this for the range.  Lyra exposes a "TX drive 0..100"
      operator surface; the protocol layer maps that onto this
      hardware range per family.

    * ``cwx_ptt_bit_position``: HL2 packs CW state bits into the
      TX I-sample's low bits during CW transmit (CLAUDE.md §3.8
      L-5).  HL2 = bit 3 (4 state bits: cwx_ptt + dot + dash +
      cwx).  ANAN = standard bit positions (3 state bits, no
      cwx_ptt).  v0.2.1 CW path reads this; protocol layer
      handles the actual packing.

    * ``ps_feedback_uses_ddc01``: True if PS feedback samples
      arrive on DDC0+DDC1 during ``(mox=True, ps_armed=True)``
      state (HL2 cntrl1=4 routing per CLAUDE.md §3.8 corrected
      entry).  False if PS feedback uses DDC2/DDC3 (older
      ANAN P1 5-DDC pattern).  v0.3 calcc consumer reads this;
      ``ddc_map(state)`` (Phase 1 deliverable) also reads it.

    Phase 0 NOTE: only ``nddc`` + ``has_onboard_codec`` +
    ``default_audio_path`` are wired live.  The rest exist so
    v0.2 / v0.3 / v0.4 callers have stable fields to consume as
    they land, without forcing a dataclass schema bump that
    would invalidate every persisted reference.
    """
    # Identity
    family_name: str

    # Wire-protocol surface
    nddc: int
    ps_feedback_uses_ddc01: bool

    # Audio routing (CLAUDE.md §13.4)
    has_onboard_codec: bool
    default_audio_path: AudioPath

    # PureSignal (CLAUDE.md §6.5, §6.7 discipline #4)
    puresignal_requires_mod: bool

    # TX hardware quirks (CLAUDE.md §3.8, §6.7 discipline #5)
    tx_attenuator_range: tuple[int, int]
    cwx_ptt_bit_position: int

    # PA-enable is dual-path on HL2: the frame-10 C3 bit-7 PA-bias
    # bit PLUS an HL2-only Apollo-tuner I2C side-channel.  When
    # True, the frame-10 bit alone may not fully key the PA on
    # community-gateware variants that gate on the Apollo path --
    # the operator-facing control must warn rather than silently
    # half-enable (the I2C side-channel is a separate later
    # change).  CLAUDE.md §15.26 PART C.
    pa_enable_uses_apollo_i2c: bool


# ──────────────────────────────────────────────────────────────────────
# HL2 capability instance.
#
# Values verified against CLAUDE.md §3 "HL2 protocol critical facts"
# (auto-loaded into Claude context) at v0.1 Phase 0 implementation
# time.  When updating any field here, also update the matching
# CLAUDE.md §3 entry -- they're the same fact stated twice and must
# stay in sync.
#
# HL2_PLUS shares all HL2 capabilities at v0.1; the HL2+ variant
# adds a higher-stability TCXO and an external-coupler-friendly PA
# but the wire-protocol + audio + PS posture are identical to HL2.
# Phase 0 maps both RadioFamily values to this same instance via
# ``Radio.capabilities``.

HL2_CAPABILITIES: RadioCapabilities = RadioCapabilities(
    family_name="Hermes Lite 2 / 2+",
    nddc=4,                                         # CLAUDE.md §3.1
    ps_feedback_uses_ddc01=True,                    # CLAUDE.md §3.8 corrected entry
    has_onboard_codec=True,                         # AK4951 onboard
    default_audio_path=AudioPath.HL2_CODEC,         # CLAUDE.md §13.1
    puresignal_requires_mod=True,                   # CLAUDE.md §6.5
    tx_attenuator_range=(-28, 31),                  # CLAUDE.md §3.8 HL2 quirks
    cwx_ptt_bit_position=3,                         # CLAUDE.md §3.8 L-5
    pa_enable_uses_apollo_i2c=True,                 # CLAUDE.md §15.26 PART C
)
