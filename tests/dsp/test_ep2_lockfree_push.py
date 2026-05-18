"""S2 — lock-free mixer->ring push (replaces the S1 lockstep test).

S2 retired the producer-paced semaphore + the `_lockstep_slot`
rendezvous entirely.  `AK4951Sink._lockstep_outbound` is now a
non-blocking push: extend the `_tx_audio` ring under its short
lock, return.  The mixer thread never blocks on the wire — that
is what removes the GIL/Qt-paint-frame cadence coupling proven in
§15.26.  These pin: the push never blocks, touches no semaphore,
accumulates into the ring, and respects `_closed`.
"""
from __future__ import annotations

import collections
import threading
import time

import numpy as np

from lyra.dsp.audio_sink import AK4951Sink


class _StubStream:
    """Minimal stream: only what AK4951Sink + the push touch.

    Deliberately has NO `_ep2_send_sem` / `_lockstep_slot` — if the
    push (or __init__) still referenced them this would AttributeError,
    which is the regression guard.
    """

    def __init__(self) -> None:
        self._tx_audio_lock = threading.Lock()
        self._tx_audio = collections.deque(maxlen=48000)
        self.inject_audio_tx = False
        self.cleared = 0

    def clear_tx_audio(self) -> None:
        self._tx_audio.clear()
        self.cleared += 1


def _frame(n: int = 126) -> np.ndarray:
    return np.full((n, 2), 0.25, dtype=np.float32)


def test_push_is_nonblocking_and_fast() -> None:
    s = _StubStream()
    sink = AK4951Sink(s, mixer=None)
    # No consumer exists at all.  The OLD code would block on the
    # lockstep slot; S2 must return immediately.
    t0 = time.monotonic()
    for _ in range(50):
        sink._lockstep_outbound(_frame())
    dt = time.monotonic() - t0
    assert dt < 0.05                       # 50 pushes, no blocking
    assert len(s._tx_audio) == 50 * 126    # all accumulated in ring


def test_push_touches_no_semaphore() -> None:
    # The stub has no _ep2_send_sem/_lockstep_slot; a single push
    # must work without them (regression guard for full retirement).
    s = _StubStream()
    sink = AK4951Sink(s, mixer=None)
    sink._lockstep_outbound(_frame())
    assert len(s._tx_audio) == 126
    assert not hasattr(s, "_ep2_send_sem")
    assert not hasattr(s, "_lockstep_slot")


def test_closed_short_circuits() -> None:
    s = _StubStream()
    sink = AK4951Sink(s, mixer=None)
    sink._closed = True
    sink._lockstep_outbound(_frame())
    assert len(s._tx_audio) == 0           # closed -> no push


def test_init_cleared_ring_and_no_lockstep_drain() -> None:
    # __init__ calls clear_tx_audio (re-arms the S2 pre-fill gate)
    # and must NOT try to drain a (now non-existent) lockstep sem.
    s = _StubStream()
    AK4951Sink(s, mixer=None)
    assert s.cleared == 1
    assert s.inject_audio_tx is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
