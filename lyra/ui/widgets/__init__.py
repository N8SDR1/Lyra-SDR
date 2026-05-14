"""Reusable Qt widgets for Lyra-SDR's panels.

Widgets here are *generic enough to be used by more than one panel*.
Single-panel widgets stay inline in the panel module that owns them
(``lyra/ui/panels.py``, ``lyra/ui/spectrum.py``, etc.); only the
genuinely-shared bits land here.
"""
from lyra.ui.widgets.stepper_readout import StepperReadout

__all__ = ["StepperReadout"]
