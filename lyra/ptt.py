"""PTT (Push-To-Talk) / MOX state machine -- v0.2.0 Phase 3 commit 3a.

Authoritative spec: ``CLAUDE.md`` §15.25 (Phase 3 Thetis
ground-truth + the locked integrated FSM design) + §6.6.

Design (locked §15.25, Plan-agent 2026-05-16)
---------------------------------------------
Single authoritative ``PttState`` driven by a **source-set +
resolver**.  Every TX trigger (MOX button, hardware PTT, CW
keyer, VOX, TUN, CAT/TCI) is a ``PttSource``; the FSM holds the
*set* of currently-held sources and ``_resolve()`` maps that set
to exactly one state with precedence ``MOX > CW > VOX > TUN >
RX``.  SW-MOX and HW-PTT therefore OR at the *intent* level,
resolve to one state, and the wire MOX bit is touched exactly
twice per transmission -- never bit-OR'd (§15.25 "shared, not
OR'd at the bit level").  Forward-compat for CW(v0.2.2) /
VOX(v0.2.3) / TUN is "add a ``PttSource`` member + one
``_resolve()`` line"; the keydown/keyup ordering, the fade
gate, the HW forwarder and the wire funnel never change.

Keydown (RX -> MOX_TX), exact §15.25 order
------------------------------------------
1. ``radio.set_mox(True)`` -- commit 2 (``eef2218``) owns the
   load-bearing TX-freq-push-BEFORE-MOX-bit inside ``set_mox``;
   the FSM MUST NOT duplicate ``_set_tx_freq``.
2. ``on_tx_state_changed(True, state)`` -- auto-mute hook
   (no-op in Phase 3; §15.14 fills the body later -- the
   ``state`` arg is defined now so CW/TUN/VOX need no
   signature change).
3. ``stream.inject_tx_iq = True``.
4. ``mox_edge_fade.start_fade_in()`` -- LAST.  inject BEFORE
   start_fade_in because ``TxDspWorker`` only pumps
   ``MoxEdgeFade.apply()`` inside its ``inject_tx_iq`` branch;
   the cos² envelope's first sample is 0 so opening the gate a
   hair early is click-free.

Keyup (MOX_TX -> RX), the load-bearing path
-------------------------------------------
1. ``mox_edge_fade.start_fade_out()``.
2. enter internal ``_releasing`` (NOT a ``PttState`` member --
   keeps the enum stable for persisted/compared values).
3. arm a 5 ms coalescing ``QTimer`` polling
   ``mox_edge_fade.is_off()`` (lock-guarded, race-free; the
   already-running ``TxDspWorker`` pumps the down-ramp).
4. on OFF -> ``_finalize_keyup``: ``inject_tx_iq = False`` ->
   ``radio.set_mox(False)`` (clear the MOX bit ONLY NOW, after
   the down-ramp fully completed -- §15.25; clearing earlier =
   key-click/splatter) -> ``on_tx_state_changed(False,state)``
   -> optional ``ptt_out_delay`` -> ``_transition(_resolve())``.

NEVER blocks the Qt main thread (§15.21 discipline); NEVER
``time.sleep`` -- all delays are single-shot ``QTimer`` (HL2
defaults are all-zero so every delay degenerates to an inline
call).

Re-key during a draining keyup (§15.25 ambiguity #1, resolved)
--------------------------------------------------------------
If a source is re-asserted while ``_releasing``, ``_finalize_
keyup`` re-checks ``_resolve()``: if it still says ``MOX_TX``
the keyup is COLLAPSED -- skip ``inject=False`` / ``set_mox
(False)``, re-``start_fade_in()``, stay ``MOX_TX``.  A sub-50 ms
MOX-bit clear+set would be exactly §15.25 traps #2/#5; collapse
mirrors ``MoxEdgeFade``'s own internal abort-continuity.

Threading
---------
Qt-main is the **sole writer** of ``_state`` / ``_active_
sources`` / ``_releasing`` -> no locks in this class.  The only
cross-thread entry is :meth:`set_hardware_ptt` (``@Slot``)
delivered via ``QueuedConnection`` from the RX-loop thread (the
HW-PTT forwarder lands in commit 3c).  Readers may sample
:attr:`current_state` from any thread (GIL-atomic enum read).

Runtime binding
---------------
The FSM is constructed before any stream exists (Radio
``__init__``, commit 3b).  Runtime refs (radio / stream /
mox_edge_fade / the auto-mute callback) are injected by
:meth:`bind_runtime` in ``Radio.start()`` and cleared by
:meth:`unbind_runtime` in ``Radio.stop()``.  With no bound
runtime the FSM still tracks state + emits ``state_changed``
(UI stays live) but skips the DSP/protocol calls and finalizes
a keyup instantly (nothing on the wire to ramp).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot


class PttState(Enum):
    """Lyra PTT runtime states (CLAUDE.md §6.6 / §15.25).

    RX is the resting state; the rest are TX.  Phase 3 makes
    only RX <-> MOX_TX live; TUN_TX/CW_TX/VOX_TX are defined-
    but-unreached so v0.2.2/2.3 wire them without an enum
    migration (a persisted/compared enum value changing is a
    breaking event -- avoid).
    """

    RX = "rx"
    MOX_TX = "mox_tx"
    TUN_TX = "tun_tx"
    CW_TX = "cw_tx"
    VOX_TX = "vox_tx"


class PttSource(Enum):
    """A held TX-intent source.  The FSM holds the *set* of
    active sources; :meth:`PttStateMachine._resolve` maps the
    set to one :class:`PttState`."""

    SW_MOX = "sw_mox"      # MOX button (commit-5 UI)
    HW_PTT = "hw_ptt"      # EP6 ptt_in (commit 3c forwarder)
    TUN = "tun"            # TUN button (v0.2.x)
    CW_KEY = "cw_key"      # CW keyer (v0.2.2)
    VOX = "vox"            # VOX subscriber (v0.2.3)
    CAT_TCI = "cat_tci"    # CAT/TCI remote PTT (later)


@dataclass(frozen=True)
class TrSequencing:
    """TR-sequencing delays (ms).  These are operator "TR
    sequencing" timing values, NOT capability-zero on HL2 (an
    earlier draft wrongly defaulted them all to zero, which
    collapsed the keyup tail fully inline and left no
    hardware-T/R settle window before the receiver was restarted
    -- the cause of the un-key transition transient).  Defaults
    here match the long-standing reference defaults: a short gap
    to let in-flight transmit samples clear before the MOX bit
    drops, and a hardware-switch settle after the MOX bit clears
    before the receiver is declared/restarted.  Operator-tunable
    later; still sourced via the capability struct per CLAUDE.md
    §6.7 #5 (the FSM stays hardware-agnostic).  Each non-zero
    delay is applied via a single-shot ``QTimer`` -- never
    ``sleep`` (Qt-main must not block, §15.21)."""

    mox_delay_ms: int = 10      # gap: down-ramp done -> clear MOX bit
                                #   (lets in-flight TX samples clear)
    ptt_out_delay_ms: int = 20  # HW-T/R settle after the MOX bit
                                #   clears, before RX is restarted
    rf_delay_ms: int = 0        # gap: MOX bit set -> start TX I/Q
    space_mox_delay_ms: int = 0  # CW inter-element hold (v0.2.2)
    key_up_delay_ms: int = 0    # CW keyer hang (v0.2.2)


# Resolver precedence: a voice/MOX source beats CW beats VOX
# beats TUN beats RX (Thetis "keying MOX off force-clears TUN/
# 2TONE" falls out of single-valued resolution -- no explicit
# force-clear path needed).
_MOX_SOURCES = frozenset({PttSource.SW_MOX, PttSource.HW_PTT,
                          PttSource.CAT_TCI})


class PttStateMachine(QObject):
    """Single-state PTT/MOX FSM (see module docstring)."""

    state_changed = Signal(object)   # PttState

    # Keyup fade-completion poll cadence.  The fade is 50 ms;
    # a 5 ms poll adds <=5 ms to MOX-bit-clear latency (far
    # below any splatter/perception threshold) for ~10 ticks
    # total per keyup, with zero cross-thread machinery.
    _FADE_POLL_MS = 5

    def __init__(self, parent=None,
                 tr_sequencing: Optional[TrSequencing] = None) -> None:
        super().__init__(parent)
        self._state: PttState = PttState.RX
        self._active_sources: "set[PttSource]" = set()
        self._releasing: bool = False
        self._tr = tr_sequencing or TrSequencing()

        # Injected runtime (None until bind_runtime; see module
        # docstring).  Duck-typed -- no Radio import (commit 3a
        # is pure / unit-testable with fakes).
        self._radio = None
        self._stream = None
        self._mox_edge_fade = None
        self._on_tx_state_changed: Optional[
            Callable[[bool, PttState], None]] = None

        # Qt-main-affinity timer for the non-blocking keyup
        # fade-gate.  Started only during a keyup; the slot
        # ``_on_fade_poll`` is also the unit-test seam (call it
        # directly -- no running event loop required in tests).
        self._fade_poll_timer = QTimer(self)
        self._fade_poll_timer.setInterval(self._FADE_POLL_MS)
        self._fade_poll_timer.setSingleShot(False)
        self._fade_poll_timer.timeout.connect(self._on_fade_poll)

    # ── runtime binding ────────────────────────────────────────────
    def bind_runtime(self, *, radio, stream, mox_edge_fade,
                      on_tx_state_changed: Callable[
                          [bool, PttState], None]) -> None:
        """Inject runtime refs (Radio.start(), commit 3b)."""
        self._radio = radio
        self._stream = stream
        self._mox_edge_fade = mox_edge_fade
        self._on_tx_state_changed = on_tx_state_changed

    def unbind_runtime(self) -> None:
        """Drop runtime refs (Radio.stop()) AND force a safe idle
        state.  SAFETY-CRITICAL (operator-reported 2026-05-16): the
        FSM object outlives the stream, so if it is left holding a
        TX source / ``MOX_TX`` state when the stream stops, a later
        start() would resume transmitting -- a radio that comes up
        keyed on restart.  A stream stop unconditionally means "not
        transmitting": clear every held source and snap back to RX
        (emitting ``state_changed`` so UI mirrors un-key), drop the
        releasing flag + poll timer.  There is no valid
        resume-TX-on-restart case."""
        self._radio = None
        self._stream = None
        self._mox_edge_fade = None
        self._on_tx_state_changed = None
        if self._fade_poll_timer.isActive():
            self._fade_poll_timer.stop()
        self._releasing = False
        self._active_sources.clear()
        if self._state != PttState.RX:
            self._state = PttState.RX
            self.state_changed.emit(PttState.RX)

    # ── queries ────────────────────────────────────────────────────
    @property
    def current_state(self) -> PttState:
        return self._state

    @property
    def is_tx(self) -> bool:
        return self._state != PttState.RX

    # ── core funnel ────────────────────────────────────────────────
    def _resolve(self) -> PttState:
        """Map the held source set to exactly one state."""
        s = self._active_sources
        if s & _MOX_SOURCES:
            return PttState.MOX_TX
        if PttSource.CW_KEY in s:
            return PttState.CW_TX
        if PttSource.VOX in s:
            return PttState.VOX_TX
        if PttSource.TUN in s:
            return PttState.TUN_TX
        return PttState.RX

    def set_source(self, source: PttSource, active: bool) -> None:
        """The core funnel.  Add/remove a held source (level-
        driven, idempotent via set semantics) then drive the
        resolved-state transition.  Qt-main only."""
        if active:
            if source in self._active_sources:
                return
            self._active_sources.add(source)
        else:
            if source not in self._active_sources:
                return
            self._active_sources.discard(source)
        self._drive_to(self._resolve())

    def _drive_to(self, target: PttState) -> None:
        """Apply the resolved target: run keydown/keyup action
        chains for the RX<->TX edges; plain transition for
        TX<->TX (Phase 3 never exercises TX<->TX -- only
        SW/HW MOX)."""
        cur = self._state
        if target == cur and not self._releasing:
            return
        if cur == PttState.RX and target != PttState.RX:
            self._enter_tx(target)
        elif cur != PttState.RX and target == PttState.RX:
            self._begin_keyup()
        else:
            # TX<->TX (e.g. TUN->MOX) or a target change while
            # already releasing: collapse handling lives in
            # _finalize_keyup; for a direct TX<->TX just
            # transition (Phase 3 unreached).
            self._transition(target)

    # ── keydown ────────────────────────────────────────────────────
    def _enter_tx(self, target: PttState) -> None:
        """RX -> TX action chain, exact §15.25 order."""
        self._transition(target)
        if self._radio is not None:
            # Commit 2 (eef2218) handles TX-freq-push BEFORE the
            # dispatch MOX flip inside set_mox -- do NOT duplicate.
            self._radio.set_mox(True)
        if self._on_tx_state_changed is not None:
            self._on_tx_state_changed(True, self._state)
        # rf_delay (HL2=0 -> inline): gap between MOX bit and
        # starting TX I/Q.
        self._deferred(self._tr.rf_delay_ms, self._open_tx_iq)

    def _open_tx_iq(self) -> None:
        if self._stream is not None:
            self._stream.inject_tx_iq = True
        if self._mox_edge_fade is not None:
            self._mox_edge_fade.start_fade_in()

    # ── keyup ──────────────────────────────────────────────────────
    def _begin_keyup(self) -> None:
        """TX -> RX: start the down-ramp, then non-blocking gate
        on its completion before clearing the MOX bit."""
        if self._mox_edge_fade is None:
            # No fade engine bound (e.g. MOX toggled pre-start) ->
            # clean instant release; nothing on the wire to ramp.
            self._finalize_keyup()
            return
        self._mox_edge_fade.start_fade_out()
        self._releasing = True
        if not self._fade_poll_timer.isActive():
            self._fade_poll_timer.start()

    @Slot()
    def _on_fade_poll(self) -> None:
        """Qt-main, every 5 ms during a keyup.  Also the unit-
        test seam (call directly)."""
        if not self._releasing:
            self._fade_poll_timer.stop()
            return
        fade = self._mox_edge_fade
        if fade is not None and not fade.is_off():
            return  # ramp still draining (<=~10 polls for 50 ms)
        self._fade_poll_timer.stop()
        self._finalize_keyup()

    def _finalize_keyup(self) -> None:
        """Down-ramp complete (or no fade bound).  §15.25 release
        tail -- + the §15.25-ambiguity-#1 re-key collapse."""
        # Re-key collapse: a source re-asserted during the drain
        # means the resolver now says a TX state -> do NOT clear
        # the MOX bit; re-fade-in and stay keyed (one continuous
        # transmission with a single cos² dip; avoids the
        # sub-50 ms MOX churn = §15.25 traps #2/#5).
        if self._resolve() != PttState.RX:
            self._releasing = False
            target = self._resolve()
            self._transition(target)
            if self._mox_edge_fade is not None:
                self._mox_edge_fade.start_fade_in()
            # inject_tx_iq stayed True throughout; MOX bit was
            # never cleared -- nothing else to do.
            return
        # Genuine keyup.
        if self._stream is not None:
            self._stream.inject_tx_iq = False
        # mox_delay (HL2=0 -> inline): gap between down-ramp-done
        # and clearing the MOX bit.
        self._deferred(self._tr.mox_delay_ms, self._clear_mox_tail)

    def _clear_mox_tail(self) -> None:
        if self._radio is not None:
            self._radio.set_mox(False)   # NOW clear the MOX bit
        # ptt_out_delay: hardware-T/R switch settle AFTER the MOX
        # bit clears and BEFORE the receiver is restarted.  The
        # receive-side hook deliberately fires in _end_keyup (past
        # this settle), NOT here -- so the RX DSP comes back only
        # once the hardware is physically on receive and the
        # antenna IQ is clean (no T/R-transition ring).
        self._deferred(self._tr.ptt_out_delay_ms, self._end_keyup)

    def _end_keyup(self) -> None:
        self._releasing = False
        self._transition(self._resolve())
        # True return-to-receive point (post HW-T/R settle):
        # restart the receive path here.
        if self._on_tx_state_changed is not None:
            self._on_tx_state_changed(False, self._state)

    # ── helpers ────────────────────────────────────────────────────
    def _deferred(self, ms: int, fn: Callable[[], None]) -> None:
        """Run ``fn`` inline if ``ms<=0`` (HL2 path), else after
        ``ms`` via a single-shot QTimer.  NEVER ``sleep`` --
        Qt-main must not block (§15.21)."""
        if ms <= 0:
            fn()
        else:
            QTimer.singleShot(int(ms), fn)

    def _transition(self, new_state: PttState) -> None:
        """Apply + emit.  Idempotent on identical state."""
        if new_state == self._state:
            return
        self._state = new_state
        self.state_changed.emit(new_state)

    # ── public API: source wrappers (scaffold-compat) ──────────────
    def request_mox(self) -> None:
        """MOX button pressed."""
        self.set_source(PttSource.SW_MOX, True)

    def release_mox(self) -> None:
        """MOX button released."""
        self.set_source(PttSource.SW_MOX, False)

    def request_tun(self) -> None:
        """TUN button pressed (v0.2.x; resolver-reachable)."""
        self.set_source(PttSource.TUN, True)

    def release_tun(self) -> None:
        """TUN button released."""
        self.set_source(PttSource.TUN, False)

    def key_down(self) -> None:
        """CW keyer element asserted (v0.2.2)."""
        self.set_source(PttSource.CW_KEY, True)

    def key_up(self) -> None:
        """CW keyer element released (v0.2.2)."""
        self.set_source(PttSource.CW_KEY, False)

    @Slot(bool)
    def set_hardware_ptt(self, active: bool) -> None:
        """Hardware PTT edge (HL2 EP6 ``ptt_in``).  ``@Slot`` so
        the commit-3c forwarder can ``QMetaObject.invokeMethod``
        it by name via ``QueuedConnection`` (RX-loop -> Qt-main).
        Shares state with SW MOX via the source set -- HW
        released while SW MOX still held stays keyed (no spurious
        keyup)."""
        self.set_source(PttSource.HW_PTT, bool(active))

    def force_release_all(self) -> None:
        """Clear every held source and drive a normal gated
        keyup.  §15.20 TX-timeout hook (the timer/QSettings/toast
        is its own Phase-3 commit; this is just the FSM entry).
        Going through the source set -- not a raw MOX clear --
        means a still-asserted HW PTT won't instantly re-key
        (it re-fires only on its next edge)."""
        if not self._active_sources and self._state == PttState.RX:
            return
        self._active_sources.clear()
        self._drive_to(self._resolve())
