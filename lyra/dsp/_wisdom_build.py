"""Throwaway WDSP FFTW-wisdom builder (subprocess entry).

Source-tree invocation:  ``python -m lyra.dsp._wisdom_build <dir>``

Loads ``wdsp.dll`` and calls ``WDSPwisdom(<dir>/)`` which runs the
one-time FFTW_PATIENT plan search AND its internal
``AllocConsole()`` + ``freopen_s(stdout,"conout$")`` +
``FreeConsole()`` stdout-hijack (WDSP ``wisdom.c:55-109``).  That
hijack permanently breaks this process's stdout — which is WHY it
must run here, in a throwaway child that has no UI and exits
immediately, NOT in the main Lyra process (where the broken stdout
aborts startup before the window appears).

The parent (``wdsp_native.ensure_wisdom``) passes a PRIVATE temp
``<dir>``; WDSP writes ``<dir>/wdspWisdom00``; the parent then
atomically ``os.replace``s it into the real cache so "file present"
always means "file valid" (a killed/concurrent child can never
leave a half-written canonical cache).

Frozen (PyInstaller ``--windowed``) builds do NOT reach this module
— ``sys.executable`` is the app exe and ``-m`` is ignored by the
bootloader, so the parent instead spawns ``<exe> --wisdom-build
<dir>`` which ``lyra/ui/app.py`` handles via an early ``sys.argv``
sentinel BEFORE QApplication/Radio/socket (preventing a recursive
second HL2-binding Lyra).  Both paths funnel into ``build()``.

Exit code: 0 iff ``<dir>/wdspWisdom00`` was produced; non-zero
otherwise.  The parent treats any non-zero as "build failed —
continue without cached wisdom (WDSP re-plans slow but safe at
channel-open); never crash, never block forever".
"""
from __future__ import annotations

import os
import sys


def build(build_dir: str) -> int:
    """Run the one-time WDSP wisdom build into ``build_dir``.

    Returns a process exit code: 0 success, non-zero failure.
    Self-contained + exception-safe — this process is disposable.
    """
    if not build_dir:
        return 2
    try:
        from lyra.dsp import wdsp_native as wn
        lib = wn.load()
        ffi = wn.ffi()
        # Trailing separator so WDSP writes "<build_dir>/wdspWisdom00"
        # INSIDE the dir (WDSP does strcat(dir,"wdspWisdom00")).
        dir_arg = os.path.join(build_dir, "")
        lib.WDSPwisdom(ffi.new("char[]", dir_arg.encode("utf-8")))
        produced = os.path.isfile(
            os.path.join(build_dir, "wdspWisdom00"))
        return 0 if produced else 3
    except Exception:  # noqa: BLE001 — disposable process
        return 4


if __name__ == "__main__":
    raise SystemExit(build(sys.argv[1] if len(sys.argv) > 1 else ""))
