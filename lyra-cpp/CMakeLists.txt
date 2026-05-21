# Lyra — Hermes Lite 2 / 2+ SDR transceiver, native C++ rebuild.
#
# Architecture (operator-locked 2026-05-20):
#   * C++23 (no Python, no GIL, anywhere)
#   * Qt 6 (Qt Quick / QML, RHI for graphics — Vulkan/Metal/OpenGL)
#   * Native UDP + OS threads for the wire path
#   * WDSP linked directly (lands in a later commit; Step 1 = window + discovery only)
#   * GPL v3+ (matches the existing Python Lyra tree + WDSP)
#
# Step 1 scope (this skeleton):
#   * Builds clean with MSVC v143 + Qt 6.11.1 MSVC 2022 64-bit binding
#   * Opens a Qt Quick window
#   * C++ UDP discovery thread broadcasts HPSDR Protocol 1 discovery
#     packets on every local NIC and surfaces any HL2 / HL2+ replies
#     in the window — proving the wire path can talk to the radio
#     from C++ with NO Python / NO GIL in the loop.

cmake_minimum_required(VERSION 3.21)

project(Lyra
    VERSION 0.0.1
    DESCRIPTION "Lyra — Hermes Lite 2/2+ SDR transceiver (C++23 / Qt6 rebuild)"
    LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_AUTOMOC ON)

# Qt 6 — bring in only the modules Step 1 needs.  More land later
# (Multimedia for audio output, Quick3D for the panadapter, etc.).
find_package(Qt6 6.11 REQUIRED COMPONENTS Core Gui Quick Network Multimedia)

# Vulkan SDK -- optional at compile time (Qt RHI dynamically loads
# the Vulkan runtime via the loader at app launch), but having the
# headers installed silences the Qt-side WrapVulkanHeaders warning
# every configure pass.  Operator-side install steps:
#   1. https://vulkan.lunarg.com/sdk/home#windows -> SDK Installer
#   2. Default install location; restart shell so VULKAN_SDK env
#      var is set.
# If the SDK is absent we silently skip -- Lyra still runs (Qt RHI
# falls back to D3D12 -> OpenGL transparently per FEATURES.md §0).
find_package(Vulkan QUIET)

qt_standard_project_setup(REQUIRES 6.5)

qt_add_executable(lyra
    src/main.cpp
    src/hl2_discovery.h
    src/hl2_discovery.cpp
    src/hl2_stream.h
    src/hl2_stream.cpp
    src/wdsp_native.h
    src/wdsp_native.cpp
    src/wdsp_engine.h
    src/wdsp_engine.cpp
    src/panadapter.h
    src/panadapter.cpp
)

# QML module — keeps Main.qml inside the binary as a Qt resource so
# we don't have to ship loose .qml files alongside the .exe.
qt_add_qml_module(lyra
    URI Lyra
    VERSION 1.0
    QML_FILES
        src/qml/Main.qml
)

target_link_libraries(lyra PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Quick
    Qt6::Network
    Qt6::Multimedia
)

# Step 2b: HL2Stream uses raw WinSock2 (sendto/recvfrom from
# dedicated OS threads -- one shared native socket) and Win32
# CreateWaitableTimerExW (HIGH_RESOLUTION 380 Hz EP2 keepalive
# cadence).  Link ws2_32 for WinSock2.  CreateWaitableTimerExW is
# in kernel32 which the MSVC toolchain links by default.
if(WIN32)
    target_link_libraries(lyra PRIVATE ws2_32)
endif()

# Step 3a: bundle the WDSP DSP engine DLLs (`wdsp.dll` +
# `libfftw3-3.dll` + `libfftw3f-3.dll` + `rnnoise.dll` +
# `specbleach.dll`) into a `_native/` subdirectory next to
# `lyra.exe` at build time.  WdspNative::load() resolves them
# from that path via AddDllDirectory + LoadLibraryExW.
#
# Provenance: DLLs originate from the upstream WDSP source
# (Warren Pratt NR0V, GPL v3+ -- link-compatible with Lyra's
# GPL v3+ license).  Bundled as binaries because the Python
# tree's _native/ already has the matching set (MSVC-built,
# x64) and re-building from source is an orthogonal task --
# CLAUDE.md §14 documents the toolchain.
# `copy_directory` auto-creates the destination directory + copies
# all files in one step.  Doing per-file `copy_if_different` calls
# fails on first build because the `_native/` dest dir doesn't
# exist yet (CMake's per-file copy doesn't create parent dirs).
add_custom_command(TARGET lyra POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory
        "$<TARGET_FILE_DIR:lyra>/_native"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CMAKE_CURRENT_SOURCE_DIR}/_native"
        "$<TARGET_FILE_DIR:lyra>/_native"
    COMMENT "Bundling WDSP DLLs into _native/"
    VERBATIM)

# MSVC: enable /utf-8 (sane source encoding) and the standards-
# conformant preprocessor (/Zc:preprocessor) — both required for
# clean C++23 + Qt headers on MSVC v143/v144.
if(MSVC)
    target_compile_options(lyra PRIVATE /utf-8 /Zc:preprocessor /W4 /permissive-)
    # Step 1 diagnostic build: leave WIN32_EXECUTABLE OFF so Qt
    # diagnostic output (QML errors, qWarning, qFatal) prints to
    # the parent console.  Flip to TRUE once the bench-validated
    # binary is stable.
    # set_target_properties(lyra PROPERTIES WIN32_EXECUTABLE TRUE)
endif()

# Install / deploy hint (used later by windeployqt to bundle Qt DLLs
# next to the .exe so the binary runs outside Qt Creator / VS).
install(TARGETS lyra
    BUNDLE  DESTINATION .
    RUNTIME DESTINATION .
)
