"""WISDOM-exit fix: subprocess-builder + ensure_wisdom plumbing.

Pins the red-team-mandated behaviors WITHOUT a real wdsp.dll:
  - the throwaway builder's arg guard;
  - frozen-aware spawn argv (the critical anti-recursion item) +
    DEVNULL/close_fds/CREATE_NO_WINDOW;
  - ensure_wisdom never spawns in a no-lib context (unit/headless);
  - single-flight: a present .building lock makes ensure_wisdom
    bail WITHOUT spawning a second multi-minute builder.
"""
from __future__ import annotations

import os
import sys

import lyra.dsp._wisdom_build as wb
import lyra.dsp.wdsp_native as wn


def test_builder_arg_guard() -> None:
    # No dir -> exit code 2, never touches the dll.
    assert wb.build("") == 2


def test_builder_module_runnable_shape() -> None:
    # Importable + has the build() entry the subprocess/app sentinel
    # both call (DRY — one funnel for source-tree -m and frozen
    # --wisdom-build).
    assert callable(wb.build)


class _FakePopen:
    last = None

    def __init__(self, argv, **kw):
        _FakePopen.last = (list(argv), dict(kw))
        self._polls = 0

    def poll(self):
        # Return 0 (success) on the 2nd poll so the wait loop exits
        # fast; the test only inspects argv/kwargs.
        self._polls += 1
        return 0 if self._polls >= 2 else None

    def kill(self):
        pass


def _patch_popen(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    _FakePopen.last = None


def test_spawn_argv_source_tree(monkeypatch) -> None:
    _patch_popen(monkeypatch)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    wn._spawn_wisdom_builder(r"C:\tmp\wb")
    argv, kw = _FakePopen.last
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "lyra.dsp._wisdom_build"]
    assert argv[3] == r"C:\tmp\wb"
    # Isolation flags (so the child's WDSP AllocConsole/freopen can
    # never inherit or flash anything at the parent).
    import subprocess as _sp
    assert kw["stdin"] == _sp.DEVNULL
    assert kw["stdout"] == _sp.DEVNULL
    assert kw["stderr"] == _sp.DEVNULL
    assert kw["close_fds"] is True
    if sys.platform.startswith("win"):
        assert kw["creationflags"] == 0x08000000  # CREATE_NO_WINDOW


def test_spawn_argv_frozen(monkeypatch) -> None:
    _patch_popen(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    wn._spawn_wisdom_builder(r"C:\tmp\wb")
    argv, _kw = _FakePopen.last
    # CRITICAL anti-recursion: frozen MUST be
    # ``<exe> --wisdom-build <dir>`` (NOT ``-m`` — the bootloader
    # ignores -m and would relaunch a 2nd full HL2-binding Lyra).
    assert argv == [sys.executable, "--wisdom-build", r"C:\tmp\wb"]
    assert "-m" not in argv


def test_ensure_wisdom_no_lib_never_spawns(monkeypatch) -> None:
    # No WDSP context => safe no-op, and MUST NOT spawn a builder.
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("ensure_wisdom spawned a builder "
                             "with no lib context")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    saved = wn._lib
    try:
        wn._lib = None
        wn._wisdom_loaded = False
        assert wn.ensure_wisdom(None) is False
    finally:
        wn._lib = saved
        wn._wisdom_loaded = False


def test_single_flight_lock_blocks_second_builder(
        monkeypatch, tmp_path) -> None:
    # A present .building lock => ensure_wisdom bails WITHOUT
    # spawning (two Lyras must not both burn minutes building).
    monkeypatch.setenv("APPDATA", str(tmp_path))
    fftw = tmp_path / "Lyra" / "fftw"
    fftw.mkdir(parents=True)
    (fftw / ".building").write_text("")          # someone is building
    assert not wn.wisdom_present()               # no cache yet

    import subprocess

    def _boom(*a, **k):
        raise AssertionError("spawned a 2nd builder despite the "
                             ".building single-flight lock")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    saved = wn._lib
    try:
        wn._lib = object()                       # non-None lib ctx
        wn._wisdom_loaded = False
        assert wn.ensure_wisdom(wn._lib) is False
        # No throwaway temp build dir was created either.
        assert not any(p.name.startswith(".wbuild-")
                       for p in fftw.iterdir())
    finally:
        wn._lib = saved
        wn._wisdom_loaded = False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
