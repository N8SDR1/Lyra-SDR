"""W1.0 — transparent pass-through ``HL2Stream`` proxy.

First sub-stage of the operator-locked process-isolation
architecture (CLAUDE.md §15.26 charter + the W1 design CONVERGED
& LOCKED through 3 red-team rounds / 4 senior agents).

**W1.0 is wire-identical BY CONSTRUCTION.**  This class is a
pure delegating shim: it contains one real ``HL2Stream`` and
forwards *every* attribute read, attribute write, attribute
delete and method call straight to it via ``__getattr__`` /
``__setattr__`` / ``__delattr__``.  It introduces NO rings, NO
extra thread, NO behaviour, NO ordering change — the contained
``HL2Stream`` runs exactly as it does today on its own internal
threads (``_rx_loop`` / ``_ep2_writer_loop``).  The only change
vs HEAD is that ``Radio`` constructs ``HL2StreamProxy(...)``
instead of ``HL2Stream(...)``; the delegation is total so the
EP2 cadence, the S2 timer-paced writer, the S3 ``_ctl_q``, the
§15.25 keydown/keyup ordering and the §15.21 teardown are
byte-identical.

It exists so the W1.1+ sub-stages can interpose ring routing at
the **stream-internal `_cc_registers`/`_tx_audio` mutation
boundary** (NOT at public method names — the red-team D-W1b
silent-W2-landmine finding) one path at a time, each
independently A/B-gated and revertable, with W1 remaining the
fallback for the W2 cross-process move.

Note (W1.0 ONLY): the contained stream IS exposed as a live
object via transparent delegation — that is correct and
required here for wire-identity.  The "do NOT expose
`_tx_audio`/`_cc_registers` as live objects; use guard objects"
requirement (red-team amendment v3-3 / A1) lands at **W1.4**,
when the tx_ring/cc_cmd routing is interposed.  W1.0 changes
nothing.
"""
from __future__ import annotations

from lyra.protocol.stream import HL2Stream

#: Instance attributes owned by the proxy itself (everything
#: else is delegated to the contained stream).
_PROXY_OWN = frozenset({"_w1_stream"})


class HL2StreamProxy:
    """Transparent delegate around one real :class:`HL2Stream`.

    Forwards the complete public *and* private surface (the
    `_tx_audio` / `_tx_audio_lock` / `_cc_registers` /
    `_set_tx_freq` / `inject_audio_tx` / `inject_tx_iq` raw
    reach-ins ``Radio`` / ``audio_sink`` / ``ptt`` perform are
    delegated verbatim).  Constructed with the exact same
    signature as ``HL2Stream``."""

    def __init__(self, *args, **kwargs) -> None:
        # object.__setattr__ to bypass our forwarding __setattr__
        # (and because the contained stream does not exist yet).
        object.__setattr__(self, "_w1_stream",
                            HL2Stream(*args, **kwargs))

    # ── total delegation ─────────────────────────────────────
    def __getattr__(self, name: str):
        # __getattr__ is only called when normal lookup misses;
        # `_w1_stream` lives in __dict__ so this never recurses.
        # A missing attr on the real stream raises AttributeError
        # naturally, so hasattr()/getattr(default) keep working.
        return getattr(self._w1_stream, name)

    def __setattr__(self, name: str, value) -> None:
        if name in _PROXY_OWN:
            object.__setattr__(self, name, value)
        else:
            # e.g. `proxy.inject_audio_tx = True`,
            # `proxy.inject_tx_iq = True` must hit the real stream.
            setattr(self._w1_stream, name, value)

    def __delattr__(self, name: str) -> None:
        if name in _PROXY_OWN:
            object.__delattr__(self, name)
        else:
            delattr(self._w1_stream, name)

    # ── explicit, non-forwarded helpers (W1.1+ use these) ────
    def unwrap(self) -> HL2Stream:
        """The contained real :class:`HL2Stream`.  W1.1+ stages
        and tests reach the concrete stream through this rather
        than relying on delegation."""
        return self._w1_stream

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<HL2StreamProxy W1.0 wrapping {self._w1_stream!r}>"
