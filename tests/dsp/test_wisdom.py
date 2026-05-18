"""FFTW WISDOM plumbing tests (no DLL required).

Pins the two operator-locked requirements:
  1. The wisdom file lives in a Lyra-PRIVATE directory and is
     NEVER any other HPSDR app's path (dual-run operators flip
     between clients — a shared file would cross-contaminate).
  2. There is a working clear/rebuild path (delete the file ->
     forced rebuild on next start), needed after a hardware
     change or a DSP-engine bump.

Plus: the cffi cdef exposes WDSPwisdom, and the per-process
idempotence guard behaves.  None of this needs wdsp.dll.
"""
from __future__ import annotations

import lyra.dsp.wdsp_native as wn


def test_cdef_exposes_wdspwisdom() -> None:
    assert "WDSPwisdom" in wn._CDEF
    assert "int  WDSPwisdom(char* directory);" in wn._CDEF


def test_wisdom_filename_is_wdsp_fixed() -> None:
    # WDSP hard-codes this; isolation is by DIRECTORY only.
    assert wn._WISDOM_FILENAME == "wdspWisdom00"
    assert wn.wisdom_file().name == "wdspWisdom00"


def test_wisdom_dir_is_lyra_private_not_thetis(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = wn.wisdom_dir()
    parts = [p.lower() for p in d.parts]
    # Under Lyra's own user-data folder, in an 'fftw' subdir.
    assert "lyra" in parts
    assert d.name == "fftw"
    assert str(tmp_path) in str(d)
    # HARD requirement: never another HPSDR app's path.
    assert "thetis" not in str(d).lower()
    assert "openhpsdr" not in str(d).lower()
    assert "powersdr" not in str(d).lower()


def test_present_delete_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # Absent initially.
    assert wn.wisdom_present() is False
    assert wn.delete_wisdom() is False          # safe when absent
    # Simulate a built cache.
    f = wn.wisdom_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"fftw-wisdom-blob")
    assert wn.wisdom_present() is True
    # Clear it (the operator Settings action).
    assert wn.delete_wisdom() is True
    assert wn.wisdom_present() is False
    assert not f.exists()


def test_delete_resets_idempotence_guard(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    wn._wisdom_loaded = True
    wn.delete_wisdom()                           # absent, still resets
    assert wn._wisdom_loaded is False


def test_ensure_wisdom_no_lib_is_safe_noop() -> None:
    # Called with no handle and none loaded -> must not raise,
    # must not claim a (re)build.
    saved = wn._lib
    try:
        wn._lib = None
        wn._wisdom_loaded = False
        assert wn.ensure_wisdom(None) is False
    finally:
        wn._lib = saved
        wn._wisdom_loaded = False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
