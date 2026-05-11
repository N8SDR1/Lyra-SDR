"""Spectrum-source-switch mixin shared by both panadapter renderers.

This module exists because Lyra has two backend-incompatible spectrum
widget implementations -- ``SpectrumWidget`` (the painted CPU/OpenGL
backend, ``lyra/ui/spectrum.py``) and ``SpectrumGpuWidget`` (the
custom GLSL backend, ``lyra/ui/spectrum_gpu.py``) -- and a v0.1
RX2 / v0.2 TX / v0.3 PS architectural requirement (consensus-plan
§9.5) that the panadapter source be switchable at runtime per the
``DispatchState`` produced by ``lyra/radio_state.py``.

The v0.0.9.9.1 EiBi-overlay regression proved that "wire it into one
backend, forget the other" is a real Lyra failure mode (see CLAUDE.md
v0.0.9.9.1 release notes).  A shared mixin defining the source-switch
contract makes both backends honor the same protocol by construction.

Authoritative spec:
``docs/architecture/v0.1_rx2_consensus_plan.md`` §3.3 M-2 (REWRITTEN
Round 3 2026-05-11 per Round 2 Agent E Gap L + Agent D B-7) and
§3.1.x item 7 (pinned Round 5 2026-05-11 per Round 4 Agent G:
"mixin is PLAIN PYTHON, not a QObject subclass").

Why the mixin is a plain Python class (not QObject)
====================================================

Qt's metaobject system **forbids multiple inheritance from two
QObject-derived bases**.  ``SpectrumWidget`` already inherits from
``_PaintedWidget`` (= ``QWidget`` or ``QOpenGLWidget`` depending on
``visuals/graphics_backend``); ``SpectrumGpuWidget`` already inherits
from ``QOpenGLWidget``.  If this mixin were also a ``QObject``,
Python's C3 linearization would produce an MRO with two QObject
ancestors and Qt would refuse to instantiate either widget at
import time -- a fatal startup error every operator would hit.

Plain Python class is the only legal answer.  Phase 0 stores the
callable + source_id and takes no further action; Phase 2 wires the
actual frame dispatch into each widget's FFT pipeline (the widget,
not the mixin, owns the FFT call -- the mixin is just the
source-switch surface).

Why push-style (not pull-style)
================================

The mixin's ``set_source(source_id, frame_dispatch_fn)`` is
**push-style**: the producer is the side that pushes frames into the
widget via ``frame_dispatch_fn``.  This avoids the widget having to
know HOW to pull from RX1 vs RX2 vs TX vs PS-feedback sources --
each of those has a different threading model (RX is in HL2Stream's
thread, TX-baseband is in WDSP's TX engine, PS-feedback is in
calcc's calibration loop).  The widget exposes the dispatch hook;
the producer owns the call-site decision.

Thread-safety: the producer that calls ``frame_dispatch_fn`` MUST
marshal onto the Qt main thread (via ``QMetaObject.invokeMethod``
or signal-slot) before invoking it.  Phase 0 has no producer wired;
Phase 2 implementers are expected to read this constraint.

Why the mixin avoids overriding ``__init__``
=============================================

Adding an ``__init__`` to the mixin forces every consumer to chain
``super().__init__(*args, **kwargs)`` correctly through the Qt MRO,
which is fragile.  Instead the mixin **lazy-initializes** its state
on first access through ``set_source`` (or the read accessors).
This keeps the mixin idempotent regardless of when in the widget's
lifecycle ``set_source`` is first called, and means consuming
widgets need zero ``__init__`` changes to adopt it.
"""
from __future__ import annotations

from enum import Enum
from typing import Callable, Optional

import numpy as np


class SourceID(Enum):
    """Identifier for the spectrum data source currently selected.

    Used by both ``SpectrumSourceMixin.set_source`` and (Phase 2+)
    the panadapter source-switch matrix in §8.5.  String values
    are stable for QSettings persistence -- don't change them
    without bumping a settings-migration version.
    """
    RX1_BAND = "rx1_band"            # DDC0 RX-band samples (current default)
    RX2_BAND = "rx2_band"            # DDC1 RX-band samples (v0.1 Phase 3 wires)
    TX_BASEBAND = "tx_baseband"      # TXA sip1 post-ALC pre-iqc (v0.2 wires)
    PS_FEEDBACK = "ps_feedback"      # DDC0/DDC1 via cntrl1=4 (v0.3 wires)


# Type alias for the frame-dispatch callable.  Producer is expected
# to call this with a complex64 IQ block (or, for the TX/PS paths,
# whatever the source produces at its native rate) -- the widget
# routes through its FFT pipeline.  None means "no frame this tick"
# (rare; producers usually just don't call rather than passing None).
FrameDispatchFn = Callable[[np.ndarray], None]


class SpectrumSourceMixin:
    """Source-switch contract shared by ``SpectrumWidget`` and
    ``SpectrumGpuWidget``.

    Mixin pattern (plain Python, NOT QObject) -- see module
    docstring for why.  Lazy-initialized; consuming widgets need
    zero ``__init__`` changes.

    Phase 0 contract:
        * ``set_source(source_id, frame_dispatch_fn)`` stores both
          values and returns.  No FFT routing wired; no producer
          dispatched.  Phase 2 lights it up.

    Phase 2+ contract (forward-looking, for documentation only):
        * Each widget's existing FFT pipeline (today driven by
          ``Radio.spectrum_ready`` Qt signal) will gain a switchable
          input.  The active source -- selected via this mixin --
          decides which producer's frames feed the FFT.  Switching
          source via ``set_source`` MUST be atomic from the operator's
          perspective: previous producer's frames stop, new
          producer's frames start, no spliced frame from both.

        * The widget owns the FFT call; the mixin owns the
          source-switch state.  Don't move FFT logic into the mixin.

    Read accessors (``active_source_id``, ``active_dispatch_fn``)
    are provided for tests + future Phase 2 wiring inspection.
    """

    # Class-level default values -- consumed by lazy-init on first
    # access.  Setting these at the class level (not in __init__)
    # avoids the Qt MRO super().__init__() chaining problem.
    _spectrum_source_id: SourceID = SourceID.RX1_BAND
    _spectrum_dispatch_fn: Optional[FrameDispatchFn] = None

    def set_source(
        self,
        source_id: SourceID,
        frame_dispatch_fn: Optional[FrameDispatchFn],
    ) -> None:
        """Switch the spectrum data source.

        Phase 0 implementation: store both values on the instance
        (lazy-initializing the per-instance attributes if this is
        the first call) and return.  No further action.

        Phase 2+ implementation will:
        1. Disconnect from the previous producer's frame-dispatch
           path (Qt signal disconnect, or producer-side route table
           update, depending on source).
        2. Connect to the new producer.
        3. Reset the widget's FFT pipeline state (drop any
           in-flight frame) so the displayed spectrum reflects
           ONLY the new source's content.

        Args:
            source_id: Which producer's data this widget should
                display.  See ``SourceID`` for the full list.
            frame_dispatch_fn: Callable the producer invokes with
                each frame; the widget routes through its FFT.
                ``None`` is legal during Phase 0 (no producer
                wired) and during Phase 2+ transitions where
                source is being changed without immediately
                attaching a new producer.

        Raises:
            TypeError: if ``source_id`` is not a ``SourceID`` enum
                member.  Defensive -- catches plan-misreadings
                (e.g., passing a bare string) before they propagate
                into Phase 2 dispatch logic.
        """
        if not isinstance(source_id, SourceID):
            raise TypeError(
                f"source_id must be a SourceID enum member, got "
                f"{type(source_id).__name__}")
        # Per-instance shadow of the class-level defaults.  Once
        # the first set_source call lands, subsequent reads see the
        # instance attribute (Python attribute resolution order:
        # instance dict before class dict).
        self._spectrum_source_id = source_id
        self._spectrum_dispatch_fn = frame_dispatch_fn

    @property
    def active_source_id(self) -> SourceID:
        """Current spectrum source ID (lazy-default: ``RX1_BAND``)."""
        return self._spectrum_source_id

    @property
    def active_dispatch_fn(self) -> Optional[FrameDispatchFn]:
        """Current frame-dispatch callable, or ``None`` if no
        producer is wired (Phase 0 state, or between source
        switches in Phase 2+).
        """
        return self._spectrum_dispatch_fn
