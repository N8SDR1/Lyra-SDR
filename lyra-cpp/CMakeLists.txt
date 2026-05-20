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
find_package(Qt6 6.11 REQUIRED COMPONENTS Core Gui Quick Network)

qt_standard_project_setup(REQUIRES 6.5)

qt_add_executable(lyra
    src/main.cpp
    src/hl2_discovery.h
    src/hl2_discovery.cpp
    src/hl2_stream.h
    src/hl2_stream.cpp
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
)

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
