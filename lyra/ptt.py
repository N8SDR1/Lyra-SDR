"""PTT (Push-To-Talk) state machine -- v0.2 TX bring-up Phase 0 stub.

Authoritative spec:
* ``CLAUDE.md`` §6.6 (PTT state machine)
* ``docs/architecture/v0.1_rx2_consensus_plan.md`` §8.x (TX bring-up)

Threading contract:

* **Qt main thread is the sole writer** of ``current_state``.  All
  setters (``request_mox``, ``release_mox``, ``key_down``, etc.)
  must be called from the Qt main thread.  Hardware-PTT edges
  observed in ``HL2Stream._rx_loop`` (the EP6 ``ptt_in`` bit at
  ``ControlBytesIn[0] & 0x01``, Phase 1 work) are forwarded via
  ``QMetaObject.invokeMethod(..., Qt.QueuedConnection)`` so the
  PTT thread is always Qt-main.
* Readers may sample ``current_state`` from any thread (CPython
  GIL makes the enum-attribute read atomic).
* State transitions emit ``state_changed`` on Qt's signal bus;
  consumers (Radio.set_mox, audio mixer fade gates, UI panels)
  connect with the default ``Qt.AutoConnection`` and receive the
  callback on the appropriate thread.

Phase 0 status (this file): **stub only**.  The class exists,
the enum is defined, and the transitions are draftedbut NO live
producer fires the transitions yet (no MOX button, no hardware
PTT input wiring, no CW keyer).  Imports are safe; instantiating
``PttStateMachine`` and connecting to ``state_changed`` is safe;
querying ``current_state`` returns ``PttState.RX`` always.

Phase 1+ wires:
* ``HL2Stream._rx_loop`` forwards EP6 PTT-in edges to
  ``PttStateMachine.set_hardware_ptt(bool)``.
* ``Radio.tx_active_changed`` Qt signal is driven by this
  state machine (transitions out of RX -> emit True; back to
  RX -> emit False).
* MOX button on TUNING panel calls ``request_mox()`` / ``release_mox()``.
* CW keyer (v0.2.1) calls ``key_down()`` / ``key_up()``.

Why this lives at ``lyra/ptt.py`` (not ``lyra/radio/ptt.py`` per
CLAUDE.md §8 aspirational layout): ``lyra/radio.py`` is the 10kLOC
Radio facade module.  Python cannot have both ``lyra/radio.py``
and ``lyra/radio/`` simultaneously without a major refactor that
breaks every ``from lyra.radio import Radio`` import in the tree.
``lyra/radio_state.py`` already established the pattern of
"Radio-adjacent code as a sibling module, not a sub-package."
PTT follows the same convention.

"""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QObject, Signal


class PttState(Enum):
    """Lyra PTT runtime states (CLAUDE.md §6.6).

    Strictly ordered "RX is the resting state, all others are
    transient TX states."  Multiple TX states exist so the audio
    chain + protocol layer can distinguish operator-intent (CW
    key down should NOT flip the SSB modulator; TUN should bypass
    the leveler; VOX should auto-release on speech-end).

    Phase 0 only defines RX and MOX_TX as live transitions; the
    rest are stubbed for Phase 2 (CW_TX in v0.2.1, VOX_TX in v0.2.2,
    TUN_TX in v0.2.0 Phase 3).
    """

    RX = "rx"            # resting state -- receive only
    MOX_TX = "mox_tx"    # operator MOX button or hardware PTT input
    TUN_TX = "tun_tx"    # TUN button -- low-power continuous-wave tune
    CW_TX = "cw_tx"      # CW keyer asserting (v0.2.1)
    VOX_TX = "vox_tx"    # VOX detected speech onset (v0.2.2)


class PttStateMachine(QObject):
    """State machine governing transitions between PTT states.

    Phase 0 stub: holds state at RX, exposes the transition API
    that Phase 1+ consumers will call, and emits a single
    ``state_changed(PttState)`` signal on every transition.  No
    consumers are wired yet -- this is greenfield infrastructure.

    Usage pattern (Phase 1+):

        ptt = PttStateMachine()
        ptt.state_changed.connect(radio.on_ptt_state_changed)
        # ... operator clicks MOX button ...
        ptt.request_mox()  # -> emits state_changed(PttState.MOX_TX)
        # ... operator releases MOX button ...
        ptt.release_mox()  # -> emits state_changed(PttState.RX)

    Idempotent: requesting the current state is a no-op (no
    signal emission).  This matters because hardware-PTT edges
    can chatter at the bit level (operator's foot switch may
    fire multiple times on a single press).
    """

    state_changed = Signal(object)   # PttState

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state: PttState = PttState.RX
        # Hardware PTT debounce -- Phase 1 will tune this once we
        # observe real foot-switch behavior.  Stub keeps the field
        # so callers can wire to it without breaking.
        self._hw_ptt_debounce_ms: int = 30

    @property
    def current_state(self) -> PttState:
        """Currently-asserted PTT state.  Read-safe from any thread
        (GIL makes the enum-attribute read atomic)."""
        return self._state

    @property
    def is_tx(self) -> bool:
        """Convenience: True iff currently in any TX state."""
        return self._state != PttState.RX

    def _transition(self, new_state: PttState) -> None:
        """Internal: apply a state transition + emit signal.
        Idempotent on identical states (no signal emission)."""
        if new_state == self._state:
            return
        self._state = new_state
        self.state_changed.emit(new_state)

    # ── Operator-driven transitions ────────────────────────────────
    def request_mox(self) -> None:
        """MOX button pressed (or hardware PTT-in asserted).

        Transition: any -> MOX_TX.  If already in a different TX
        state (CW_TX, TUN_TX), MOX takes precedence per §6.6 --
        operator wants voice now, regardless of prior automation.
        """
        self._transition(PttState.MOX_TX)

    def release_mox(self) -> None:
        """MOX button released (or hardware PTT-in deasserted).

        Transition: MOX_TX -> RX.  No-op if not currently in MOX_TX
        (e.g. CW keyer asserted in parallel) -- the CW keyer's
        release drives that transition independently.
        """
        if self._state == PttState.MOX_TX:
            self._transition(PttState.RX)

    def request_tun(self) -> None:
        """TUN button pressed -- low-power continuous-wave tune
        for ATU operation.  Transition: any -> TUN_TX."""
        self._transition(PttState.TUN_TX)

    def release_tun(self) -> None:
        """TUN button released.  Transition: TUN_TX -> RX."""
        if self._state == PttState.TUN_TX:
            self._transition(PttState.RX)

    def key_down(self) -> None:
        """CW keyer dot/dash element asserted (v0.2.1 wiring).

        Phase 0 stub: defined but no caller yet.  v0.2.1's CW
        keyer module will fire this on every keyer-event edge.
        """
        self._transition(PttState.CW_TX)

    def key_up(self) -> None:
        """CW keyer element released.  Transition: CW_TX -> RX
        (after the keyer's hang-time expires; v0.2.1 wiring)."""
        if self._state == PttState.CW_TX:
            self._transition(PttState.RX)

    def set_hardware_ptt(self, active: bool) -> None:
        """Hardware PTT input edge (HL2 EP6 ``ptt_in`` bit).

        Phase 0 stub: defined but no caller yet.  Phase 1 wires
        ``HL2Stream._rx_loop`` to forward the EP6 ControlBytesIn[0]
        & 0x01 bit edge here via QueuedConnection so this method
        always runs on Qt main thread.

        Treats hardware PTT as a MOX-equivalent intent.  No
        debounce yet (Phase 1 adds the ``_hw_ptt_debounce_ms``
        window once we observe real foot-switch chatter).
        """
        if active:
            self.request_mox()
        else:
            self.release_mox()
