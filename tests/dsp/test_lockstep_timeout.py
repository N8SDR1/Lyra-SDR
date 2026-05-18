"""S1 — bounded lockstep rendezvous (kills the latent infinite-hang).

The mixer thread used to ``_lockstep_slot.acquire()`` with NO
timeout: if the EP2 writer wedged, the mixer parked there FOREVER
(a permanent stuck/silent state — the worst-case the audit
flagged).  S1 bounds it to the EP2 keepalive fence.  Under healthy
cadence the writer releases long before that, so the timeout is
DORMANT and behavior is byte-identical; only the pathological case
turns from "park forever" into "skip the rendezvous, stay alive".

These pin: health = no timeout / counter stays 0; wedged writer =
returns within the bound + counter increments + NO hang.
"""
from __future__ import annotations

import collections
import threading
import time

import numpy as np

import lyra.dsp.audio_sink as asnk
from lyra.dsp.audio_sink import AK4951Sink


class _StubStream:
    def __init__(self) -> None:
        self._tx_audio_lock = threading.Lock()
        self._tx_audio = collections.deque()
        self._ep2_send_sem = threading.Semaphore(0)
        self._lockstep_slot = threading.Semaphore(0)
        self.inject_audio_tx = False

    def clear_tx_audio(self) -> None:
        self._tx_audio.clear()


def _frame() -> np.ndarray:
    return np.zeros((126, 2), dtype=np.float32)


def test_health_path_no_timeout_no_counter() -> None:
    s = _StubStream()
    sink = AK4951Sink(s, mixer=None)
    # Writer "already sent": a token is waiting -> acquire() returns
    # immediately, exactly like the old unbounded behavior.
    s._lockstep_slot.release()
    t0 = time.monotonic()
    sink._lockstep_outbound(_frame())
    dt = time.monotonic() - t0
    assert sink._lockstep_timeouts == 0
    assert dt < 0.02                       # returned promptly
    # Audio was still queued for the writer.
    assert len(s._tx_audio) == 126


def test_wedged_writer_returns_bounded_and_counts(monkeypatch) -> None:
    # Shrink the bound so the test is fast; same code path.
    monkeypatch.setattr(asnk, "_LOCKSTEP_ACQUIRE_TIMEOUT_S", 0.05)
    s = _StubStream()
    sink = AK4951Sink(s, mixer=None)
    # Never release the slot => the OLD code would hang forever.
    t0 = time.monotonic()
    sink._lockstep_outbound(_frame())
    dt = time.monotonic() - t0
    assert sink._lockstep_timeouts == 1
    assert 0.04 <= dt < 0.5                # bounded, not infinite
    # Chunk still queued — dropping the rendezvous loses no audio.
    assert len(s._tx_audio) == 126


def test_repeated_wedge_keeps_counting_no_hang(monkeypatch) -> None:
    monkeypatch.setattr(asnk, "_LOCKSTEP_ACQUIRE_TIMEOUT_S", 0.02)
    s = _StubStream()
    sink = AK4951Sink(s, mixer=None)
    for _ in range(3):
        sink._lockstep_outbound(_frame())
    assert sink._lockstep_timeouts == 3


def test_default_bound_equals_keepalive_fence() -> None:
    # The bound must equal the EP2 keepalive fence (0.050 s) so it
    # is dormant in health and only fires on a real long stall.
    assert asnk._LOCKSTEP_ACQUIRE_TIMEOUT_S == 0.050


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
