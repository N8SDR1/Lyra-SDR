"""W1.0 — the transparent pass-through HL2StreamProxy.

Pins WIRE-IDENTITY BY CONSTRUCTION: total delegation (every
public + the codebase's private reach-ins forward to one
contained real HL2Stream), no shadowing methods of its own, the
raw `_tx_audio`/`_tx_audio_lock` reach-in returns the SAME object
(so audio_sink's `self._stream._tx_audio.extend(...)` is
byte-identical), attribute writes (`inject_tx_iq`/
`inject_audio_tx`) forward, hasattr/AttributeError semantics
preserved, and Radio constructs the proxy.  No socket: HL2Stream
binds only in start(), never called here.
"""
from __future__ import annotations

import unittest

from lyra.protocol.stream import HL2Stream
from lyra.ipc.hl2_proxy import HL2StreamProxy

# The private + public surface Radio / audio_sink / ptt /
# tx_dsp_worker reach on the stream object (grep-derived).
_USED_SURFACE = (
    "start", "stop",
    "queue_tx_audio", "queue_tx_iq", "clear_tx_audio",
    "clear_tx_iq", "fade_and_replace_tx_audio",
    "set_lna_gain_db", "set_tx_step_attn_db", "set_tx_drive_level",
    "set_pa_on", "set_sample_rate", "register_mic_consumer",
    "_send_cc", "_set_tx_freq",
    "_tx_audio", "_tx_audio_lock",
    "inject_audio_tx", "inject_tx_iq", "stats",
)


class HL2StreamProxyW10Test(unittest.TestCase):
    def _pair(self):
        # __init__ binds NO socket / starts NO thread (HL2Stream
        # does that in start(), line ~2851 — never called here),
        # so there is nothing to tear down.
        real = HL2Stream("10.10.30.100", sample_rate=96000)
        proxy = HL2StreamProxy("10.10.30.100", sample_rate=96000)
        return real, proxy

    def test_used_surface_resolves_through_proxy(self):
        real, proxy = self._pair()
        for name in _USED_SURFACE:
            self.assertTrue(hasattr(real, name),
                            f"test stale: HL2Stream lacks {name!r}")
            self.assertTrue(hasattr(proxy, name),
                            f"proxy does not forward {name!r}")

    def test_methods_are_the_contained_streams_bound_methods(self):
        # Proves "delegates straight through" for the paths NOT yet
        # interposed.  start/stop ARE intercepted as of W1.1 (the
        # rx_iq seam) — excluded here, covered by the W1.1 tests.
        # queue_tx_audio/_send_cc/_set_tx_freq/set_pa_on are still
        # pure delegation (W1.3/W1.4 territory, not yet routed).
        _real, proxy = self._pair()
        inner = proxy.unwrap()
        for name in ("queue_tx_audio",
                     "_send_cc", "_set_tx_freq", "set_pa_on"):
            m = getattr(proxy, name)
            self.assertTrue(callable(m))
            self.assertIs(m.__self__, inner,
                          f"{name}: not bound to the contained stream")
            # not defined on the proxy class — comes via __getattr__
            self.assertIsNone(getattr(HL2StreamProxy, name, None),
                              f"{name}: proxy must NOT shadow it")

    def test_raw_reachin_returns_the_same_object(self):
        # audio_sink does `self._stream._tx_audio_lock` /
        # `self._stream._tx_audio.extend(...)` — these MUST be the
        # contained stream's identical objects or W1.0 is not
        # wire-identical.
        _real, proxy = self._pair()
        inner = proxy.unwrap()
        self.assertIs(proxy._tx_audio, inner._tx_audio)
        self.assertIs(proxy._tx_audio_lock, inner._tx_audio_lock)
        # mutating through the proxy mutates the contained deque
        before = len(inner._tx_audio)
        proxy._tx_audio.append((0.0, 0.0))
        self.assertEqual(len(inner._tx_audio), before + 1)

    def test_attribute_writes_forward(self):
        _real, proxy = self._pair()
        inner = proxy.unwrap()
        proxy.inject_tx_iq = True
        proxy.inject_audio_tx = True
        self.assertIs(inner.inject_tx_iq, True)
        self.assertIs(inner.inject_audio_tx, True)
        # read-back through the proxy too
        self.assertIs(proxy.inject_tx_iq, True)
        proxy.inject_tx_iq = False
        self.assertIs(inner.inject_tx_iq, False)

    def test_instance_dict_parity(self):
        # Every instance attribute HL2Stream.__init__ sets must be
        # reachable + identical through the proxy (no swallowed
        # state).  vars() is side-effect-free (no property eval).
        real, proxy = self._pair()
        for name, val in vars(real).items():
            got = getattr(proxy, name)
            inner_val = getattr(proxy.unwrap(), name)
            self.assertIs(got, inner_val,
                          f"{name}: proxy did not delegate identically")

    def test_hasattr_and_attributeerror_semantics(self):
        _real, proxy = self._pair()
        self.assertFalse(hasattr(proxy, "definitely_not_a_real_attr"))
        with self.assertRaises(AttributeError):
            _ = proxy.definitely_not_a_real_attr
        self.assertEqual(getattr(proxy, "nope", "dflt"), "dflt")

    def test_truthiness_matches_a_real_stream(self):
        # radio.py:`if self._stream:` — proxy must be truthy like
        # a real HL2Stream (neither defines __bool__/__len__).
        real, proxy = self._pair()
        self.assertTrue(bool(real))
        self.assertTrue(bool(proxy))

    def test_unwrap_is_the_contained_hl2stream(self):
        _real, proxy = self._pair()
        self.assertIsInstance(proxy.unwrap(), HL2Stream)

    def test_radio_constructs_the_proxy(self):
        import lyra.radio as radio_mod
        self.assertIs(radio_mod.HL2StreamProxy, HL2StreamProxy)
        import inspect
        src = inspect.getsource(radio_mod.Radio.start)
        self.assertIn("HL2StreamProxy(", src)
        self.assertNotIn("self._stream = HL2Stream(", src)


if __name__ == "__main__":
    unittest.main()
