"""Temporary TX bring-up diagnostics (env-gated: ``LYRA_TX_DEBUG=1``).

Zero cost when the env var is unset (one bool check).  Instruments
every link of the first-RF chain so a hardware run shows exactly
where transmit breaks:

    _open_tx_channel -> TxDspWorker start -> mic-source wire ->
    FSM bind_runtime -> TUN/MOX keydown -> inject_tx_iq flip ->
    worker produces I/Q -> queue_tx_iq packs into EP2

Run with::

    set LYRA_TX_DEBUG=1
    python -u -m lyra.ui.app > "%USERPROFILE%\\lyra_console.log" 2>&1

DELETE this module + its call sites once first RF is verified.
"""
from __future__ import annotations

import os
import sys
import time

_ON = bool(os.environ.get("LYRA_TX_DEBUG"))
_last: dict[str, float] = {}


def txdbg(msg: str, *, every_s: float | None = None,
          key: str = "") -> None:
    """Print one ``[TXDBG]`` line to stdout (flushed) when
    ``LYRA_TX_DEBUG`` is set.  ``every_s`` rate-limits a hot
    call site (keyed by ``key``) so per-block sites don't flood.
    """
    if not _ON:
        return
    if every_s is not None:
        now = time.monotonic()
        if now - _last.get(key, 0.0) < every_s:
            return
        _last[key] = now
    print(f"[TXDBG] {msg}", file=sys.stdout, flush=True)
