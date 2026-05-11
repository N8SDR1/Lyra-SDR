"""Sentinel test for v0.1 Phase 0 item 8 (RadioCapabilities + HL2).

Per consensus-plan §3.1.x item 8:

  > ``lyra/protocol/capabilities.py`` (NEW file) defines
  > ``RadioCapabilities`` dataclass + ``HL2Capabilities`` populated
  > instance.  ``Radio.capabilities`` returns it.  Phase 0 reads
  > only ``nddc`` + ``default_audio_path`` + ``has_onboard_codec``
  > from the struct; rest stubbed for v0.4 expansion.

Asserts:

1. The dataclass is ``frozen`` -- UI code can pass references
   without worrying about mutation.
2. ``HL2_CAPABILITIES`` populated with values that match CLAUDE.md
   §3 / §13.4 / §6.5 / §6.7 (single source of truth for HL2 facts).
3. ``Radio.capabilities`` returns the HL2 instance.
4. The Phase 0 read-only triple (``nddc``, ``has_onboard_codec``,
   ``default_audio_path``) is consumable.
5. The "for v0.2 / v0.3 / v0.4" fields exist with the right types
   so future callers don't get TypeError on read.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError

from lyra.protocol.capabilities import (
    AudioPath, HL2_CAPABILITIES, RadioCapabilities,
)


class CapabilitiesUnitTest(unittest.TestCase):
    def test_dataclass_is_frozen(self) -> None:
        """Mutation must be rejected at the language level."""
        with self.assertRaises(FrozenInstanceError):
            HL2_CAPABILITIES.nddc = 99           # type: ignore[misc]

    def test_hl2_phase0_triple(self) -> None:
        """The three fields Phase 0 callers actually consume,
        verified against CLAUDE.md §3.1 + §13.4."""
        self.assertEqual(HL2_CAPABILITIES.nddc, 4)
        self.assertTrue(HL2_CAPABILITIES.has_onboard_codec)
        self.assertEqual(
            HL2_CAPABILITIES.default_audio_path,
            AudioPath.HL2_CODEC,
        )

    def test_hl2_full_field_population(self) -> None:
        """v0.2 / v0.3 / v0.4 fields exist with correct types so
        consumers landing in those phases don't trip on missing
        attributes.  Values verified against CLAUDE.md §3.8 / §6.5.
        """
        c = HL2_CAPABILITIES
        self.assertIsInstance(c.family_name, str)
        self.assertTrue(c.ps_feedback_uses_ddc01)          # §3.8 corrected
        self.assertTrue(c.puresignal_requires_mod)          # §6.5
        self.assertEqual(c.tx_attenuator_range, (-28, 31))  # §3.8 HL2 quirks
        self.assertEqual(c.cwx_ptt_bit_position, 3)         # §3.8 L-5

    def test_audio_path_enum_stable_values(self) -> None:
        """String values are stable for QSettings persistence per
        the module docstring -- this test pins them so a rename
        breaks loudly rather than silently invalidating saved
        operator preferences.
        """
        self.assertEqual(AudioPath.HL2_CODEC.value, "hl2_codec")
        self.assertEqual(AudioPath.PC_SOUND.value, "pc_sound")


class RadioCapabilitiesAccessorTest(unittest.TestCase):
    """``Radio.capabilities`` returns the HL2 instance in Phase 0."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def test_radio_capabilities_returns_hl2(self) -> None:
        """Phase 0 always returns HL2 -- v0.4 will wire
        discovery-driven selection.
        """
        from lyra.radio import Radio
        r = Radio()
        self.assertIs(r.capabilities, HL2_CAPABILITIES)

    def test_phase0_consumers_can_read_their_triple(self) -> None:
        """The smoke test the plan's §3.1.x item 8 envisioned:
        Phase 0 code path reads nddc + has_onboard_codec +
        default_audio_path without exception.
        """
        from lyra.radio import Radio
        r = Radio()
        caps = r.capabilities
        nddc = caps.nddc
        has_codec = caps.has_onboard_codec
        default_path = caps.default_audio_path
        self.assertEqual(nddc, 4)
        self.assertTrue(has_codec)
        self.assertEqual(default_path, AudioPath.HL2_CODEC)


if __name__ == "__main__":
    unittest.main()
