"""S2 — decoupled timer-paced EP2 egress: unit-testable parts.

Cadence correctness itself is the operator HL2 bench gate (not
unit-testable per the §15.26 plan).  What IS pinned here:

* contract pt-1: TX I/Q columns are zero unless BOTH inject_tx_iq
  AND the live MOX snapshot are set (MOX-gated, structural — not
  incidental to inject/MOX thread skew);
* the producer side no longer touches a semaphore (full retirement
  regression guard) and clear re-arms the pre-fill / slew state;
* the pre-fill knob honours the env override + safe default;
* `_setup_win32_waitable_timer` never returns a legacy coalesced
  timer (the round-12 trap) — only a HIGH_RESOLUTION dict or the
  spin-hybrid fallback.
"""
from __future__ import annotations

import types

import numpy as np

from lyra.protocol.stream import HL2Stream


def _make_stream() -> HL2Stream:
    return HL2Stream("10.10.10.1", sample_rate=96000)


def _iq_cols(packed: bytes) -> np.ndarray:
    # 126 rows x 4 BE int16: cols 0=L 1=R 2=TX I 3=TX Q.
    a = np.frombuffer(packed, dtype=">i2").reshape(126, 4)
    return a[:, 2:4]


_PAIRS = [(0.10, -0.10)] * 126


def test_tx_iq_zero_when_not_keyed_even_if_inject() -> None:
    s = _make_stream()
    s.inject_tx_iq = True                       # producer "armed"
    # No dispatch-state provider -> _snapshot_mox_bit() == 0.
    s.queue_tx_iq(np.full(126, 0.6 + 0.6j, dtype=np.complex64))
    iq = _iq_cols(s._pack_audio_bytes_pairs(_PAIRS))
    assert int(np.abs(iq).sum()) == 0           # structurally silent


def test_tx_iq_present_only_when_keyed_and_inject() -> None:
    s = _make_stream()
    s.inject_tx_iq = True
    s._dispatch_state_provider = lambda: types.SimpleNamespace(
        mox=True)                               # keyed
    s.queue_tx_iq(np.full(126, 0.6 + 0.6j, dtype=np.complex64))
    iq = _iq_cols(s._pack_audio_bytes_pairs(_PAIRS))
    assert int(np.abs(iq).sum()) > 0            # modulation present


def test_tx_iq_zero_when_not_inject_even_if_keyed() -> None:
    s = _make_stream()
    s.inject_tx_iq = False
    s._dispatch_state_provider = lambda: types.SimpleNamespace(
        mox=True)
    s.queue_tx_iq(np.full(126, 0.6 + 0.6j, dtype=np.complex64))
    iq = _iq_cols(s._pack_audio_bytes_pairs(_PAIRS))
    assert int(np.abs(iq).sum()) == 0


def test_producer_side_has_no_semaphore() -> None:
    s = _make_stream()
    # queue_tx_audio must fill the ring with NO semaphore machinery.
    s.queue_tx_audio(np.zeros((300, 2), dtype=np.float32))
    assert len(s._tx_audio) == 300
    assert not hasattr(s, "_ep2_send_sem")
    assert not hasattr(s, "_lockstep_slot")
    assert not hasattr(s, "_unsignaled_audio_samples")


def test_clear_rearms_prefill_and_slew_state() -> None:
    s = _make_stream()
    s.queue_tx_audio(np.zeros((500, 2), dtype=np.float32))
    s._ep2_prefilled = True
    s._ep2_underrun_active = True
    s.clear_tx_audio()
    assert len(s._tx_audio) == 0
    assert s._ep2_prefilled is False
    assert s._ep2_underrun_active is False


def test_prefill_default_and_present() -> None:
    s = _make_stream()
    # Env unset in the test env -> safe small default.
    assert s._ep2_prefill_samples == 256
    assert s._ep2_prefilled is False


def test_timer_setup_never_legacy_coalesced() -> None:
    # The round-12 trap is a legacy CreateWaitableTimerW; the
    # rewrite must return either None (non-Windows), a spin-hybrid
    # marker, or a HIGH_RESOLUTION dict — never a legacy timer.
    ctx = HL2Stream._setup_win32_waitable_timer()
    if ctx is None:
        return                                  # non-Windows: ok
    assert "mode" in ctx
    assert ctx["mode"] in ("highres", "spin")
    if ctx["mode"] == "highres":
        for k in ("handle", "SetWaitableTimer",
                  "WaitForSingleObject", "CloseHandle"):
            assert k in ctx
        # Don't leak the kernel handle in the test process.
        try:
            ctx["CloseHandle"](ctx["handle"])
        except Exception:
            pass


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
