# Lyra — C++23 / Qt 6 Rebuild

Native rebuild of [Lyra](../lyra) (the Python+Qt6 desktop SDR transceiver
for the Hermes Lite 2 / 2+).  The Python tree is preserved in `../lyra/`
as the protocol research + doc archive reference; this is a clean ground-
up rewrite using the architecture the project should have started with.

## Architecture

| Layer          | Choice                                                           |
|----------------|------------------------------------------------------------------|
| Language       | C++23                                                            |
| UI framework   | Qt 6 (Qt Quick / QML)                                            |
| Graphics       | Qt RHI (Vulkan/D3D12 on Windows, Metal on macOS, OpenGL fallback) |
| DSP            | WDSP linked directly (GPL v3+) — *lands in a later commit*       |
| FFT            | FFTW3 — *lands in a later commit*                                |
| Wire I/O       | Native UDP (`QUdpSocket`) on dedicated OS threads — no GIL       |
| Threading      | `std::jthread` + Qt thread pools, OS-priority + MMCSS later      |
| Build          | CMake 3.21+                                                      |
| Compiler       | MSVC v143 (VS 2022/2026) on Windows; clang/gcc on Linux; Apple Clang on macOS |
| License        | GPL v3+                                                          |

**No Python. No GIL. No cffi. No in-process bottleneck on the wire path.**

## Step 1 scope (this commit)

* Builds clean with MSVC v143 + Qt 6.11.1 MSVC 2022 64-bit binding
* Opens a Qt Quick / QML window backed by RHI
* C++ HPSDR Protocol 1 discovery on a dedicated worker thread, multi-NIC
  (binds + broadcasts from every local IPv4 interface)
* Surfaces any HL2 / HL2+ replies in the window with their IP, MAC,
  board name, gateware version, busy flag, and DDC count

This proves the toolchain end-to-end and proves the wire path can talk
to the radio from C++ with no Python in the loop.  Everything else
(DSP, audio, panadapter, TX, etc.) lands in subsequent commits, each
gated by an operator HL2 bench.

## Prerequisites (Windows)

* **Visual Studio 2022 or 2026 Community** with the "Desktop development
  with C++" workload + **MSVC v143** toolchain + Windows 11 SDK + CMake.
* **Qt 6.11.1** installed at `C:\Qt\6.11.1\` with the **MSVC 2022 64-bit**
  binding (add via Qt's `MaintenanceTool.exe` if the curated installer
  only pulled MinGW).

## Build (Windows command-line)

From an **x64 Native Tools Command Prompt for VS 2026**:

```bat
cd Y:\Claude local\SDRProject\lyra-cpp
cmake -B build -G Ninja ^
      -DCMAKE_BUILD_TYPE=Release ^
      -DCMAKE_PREFIX_PATH=C:/Qt/6.11.1/msvc2022_64
cmake --build build
build\lyra.exe
```

Or **Visual Studio 2026**: File -> Open -> CMake -> point at `CMakeLists.txt`
in this directory.  VS auto-configures, then F5 to build + run.

Or **Qt Creator**: File -> Open File or Project -> pick `CMakeLists.txt`,
let it configure, hit the green Run button.

## License

GPL v3+, matches the parent `Lyra` Python project (`../lyra/`) and WDSP
(`../lyra/dsp/_native/`, when integrated in a later commit).  See
`../LICENSE` and `../NOTICE.md`.
