// Lyra — Step 2b QML shell.
//
// Discovery (Step 1) + Stream open/close + live EP6 RX stats (Step 2a)
// + EP2 keepalive TX stats (Step 2b).  Click "Discover HL2" to scan
// the LAN; click "Open" on a found radio to start the bidirectional
// stream (each direction on its own dedicated OS thread); the banner
// shows live RX dg/s + total + dropouts + framing on the left and
// TX dg/s + total + send-errors on the right.  Click "Close" to stop.
// No DSP, no audio, no panadapter yet — Step 2b proves the gateware
// watchdog stays satisfied, the stream runs indefinitely.

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: root
    width: 1000
    height: 680
    visible: true
    title: qsTr("Lyra — Hermes Lite 2 / 2+ — v0.0.4 (C++23 / Qt 6)")

    // ---- top bar ----------------------------------------------
    header: ToolBar {
        RowLayout {
            anchors.fill: parent
            anchors.margins: 8
            spacing: 12

            Button {
                id: scanBtn
                text: qsTr("Discover HL2")
                enabled: !Stream.running
                onClicked: {
                    // Refresh the radio LIST, but keep the Log as an
                    // append-only running history.  Clearing it here
                    // wiped persistent context (e.g. the [wdsp] channel
                    // status logged once at startup).  A separator
                    // keeps repeated scans readable.
                    radioModel.clear()
                    if (logArea.text.length > 0)
                        logArea.text += "──── rescan ────\n"
                    statusLabel.text = qsTr("Scanning...")
                    Discovery.scan(1.5, 2)
                }
            }
            Label {
                id: statusLabel
                text: qsTr("Click \"Discover HL2\" to scan the LAN.")
                Layout.fillWidth: true
                elide: Label.ElideRight
            }
        }
    }

    // ---- top-level column: stream banner (when running) + split
    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Live stream-stats banner.  Only visible while a stream is
        // open.  Two-row layout: RX (radio→host EP6) on top, TX
        // (host→radio EP2 keepalive) on bottom.  Updates at 5 Hz
        // from the worker-thread atomics; the wire path itself
        // runs on its own dedicated OS threads at full line rate.
        Rectangle {
            id: streamBanner
            Layout.fillWidth: true
            implicitHeight: visible ? 64 : 0
            visible: Stream.running
            color: "#0a2a0a"
            border.color: "#2a6a2a"
            border.width: 1
            ColumnLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                anchors.topMargin: 4
                anchors.bottomMargin: 4
                spacing: 2

                // RX row -- radio -> host EP6 data
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 18
                    Label {
                        text: qsTr("● RX  ") + Stream.targetIp
                        color: "#7fff7f"
                        font.bold: true
                        font.family: "Consolas"
                    }
                    Label {
                        text: Stream.datagramsPerSec.toFixed(0) +
                              qsTr(" dg/s")
                        color: "#cccccc"
                        font.family: "Consolas"
                    }
                    Label {
                        text: Stream.totalDatagrams + qsTr(" total")
                        color: "#cccccc"
                        font.family: "Consolas"
                    }
                    Label {
                        // Count of sequence-number anomaly EVENTS
                        // (one per occurrence, not summed by gap
                        // magnitude — see hl2_stream.cpp comment).
                        text: Stream.seqErrors + qsTr(" seq err")
                        color: Stream.seqErrors > 0 ? "#ffd07f" : "#888"
                        font.family: "Consolas"
                    }
                    Label {
                        text: Stream.framingErrors + qsTr(" framing")
                        color: Stream.framingErrors > 0
                               ? "#ff7f7f" : "#888"
                        font.family: "Consolas"
                    }
                    // RX1 dBFS — first real radio reception lifted
                    // into the C++ build (Step 2c).  Sentinel
                    // -200.0 means "no samples yet"; show "—.— dBFS"
                    // until the first ~50 ms RMS window completes.
                    Label {
                        text: {
                            if (Stream.rx1DbFs <= -190.0) {
                                return qsTr("RX1 —.— dBFS")
                            }
                            return qsTr("RX1 ") +
                                   Stream.rx1DbFs.toFixed(1) +
                                   qsTr(" dBFS")
                        }
                        // Color-code by signal strength: dim grey
                        // for noise-floor territory, white for
                        // moderate, cyan for strong, amber when
                        // pushing the ADC, red for overload risk.
                        color: {
                            var v = Stream.rx1DbFs
                            if (v <= -190.0) return "#888"
                            if (v < -80.0) return "#999"
                            if (v < -50.0) return "#dddddd"
                            if (v < -20.0) return "#7fdfff"
                            if (v <  -5.0) return "#ffd07f"
                            return "#ff7f7f"
                        }
                        font.family: "Consolas"
                        font.bold: true
                    }
                    // AUDIO dBFS — peak level OUT of the WDSP DSP
                    // chain (Step 3d).  Proves IQ -> fexchange0 ->
                    // audio is flowing; rises when tuned onto a
                    // signal.  No playback yet (that's Step 3e).
                    // Sentinel -200.0 = "no audio produced yet".
                    Label {
                        text: {
                            if (WdspEngine.audioDbFs <= -190.0) {
                                return qsTr("AUDIO —.— dBFS")
                            }
                            return qsTr("AUDIO ") +
                                   WdspEngine.audioDbFs.toFixed(1) +
                                   qsTr(" dBFS")
                        }
                        color: {
                            var v = WdspEngine.audioDbFs
                            if (v <= -190.0) return "#888"
                            if (v < -60.0) return "#999"
                            if (v < -30.0) return "#dddddd"
                            if (v < -12.0) return "#7fdfff"
                            if (v <  -3.0) return "#ffd07f"
                            return "#ff7f7f"
                        }
                        font.family: "Consolas"
                        font.bold: true
                    }
                    Item { Layout.fillWidth: true }
                    Label {
                        // ~5052 dg/s expected at 192 kHz nddc=4.
                        // Distinguish the three failure modes
                        // (starting up / RX stalled while TX is
                        // healthy / rate mismatch) so the operator
                        // sees what's actually going on.
                        text: {
                            if (Stream.datagramsPerSec >= 4800 &&
                                Stream.datagramsPerSec <= 5300) {
                                return qsTr("WIRE OK")
                            }
                            if (Stream.datagramsPerSec === 0 &&
                                Stream.txDatagramsPerSec >= 300) {
                                return qsTr("RX stalled")
                            }
                            if (Stream.datagramsPerSec === 0) {
                                return qsTr("starting...")
                            }
                            return qsTr("rate off")
                        }
                        color: {
                            if (Stream.datagramsPerSec >= 4800 &&
                                Stream.datagramsPerSec <= 5300) {
                                return "#7fff7f"
                            }
                            if (Stream.datagramsPerSec === 0 &&
                                Stream.txDatagramsPerSec >= 300) {
                                return "#ff7f7f"
                            }
                            if (Stream.datagramsPerSec === 0) {
                                return "#ffd07f"
                            }
                            return "#ff7f7f"
                        }
                        font.family: "Consolas"
                        font.bold: true
                    }
                }

                // TX row -- host -> radio EP2 keepalive
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 18
                    Label {
                        text: qsTr("● TX  keepalive")
                        color: "#7fff7f"
                        font.bold: true
                        font.family: "Consolas"
                    }
                    Label {
                        text: Stream.txDatagramsPerSec.toFixed(0) +
                              qsTr(" dg/s")
                        color: "#cccccc"
                        font.family: "Consolas"
                    }
                    Label {
                        text: Stream.txTotalDatagrams + qsTr(" total")
                        color: "#cccccc"
                        font.family: "Consolas"
                    }
                    Label {
                        text: Stream.txSendErrors + qsTr(" errors")
                        color: Stream.txSendErrors > 0
                               ? "#ff7f7f" : "#888"
                        font.family: "Consolas"
                    }
                    Item { Layout.fillWidth: true }
                    Label {
                        // 380 Hz expected (= 48 kHz audio / 126
                        // samples per datagram).  Tight ±10 % band.
                        text: {
                            if (Stream.txDatagramsPerSec >= 340 &&
                                Stream.txDatagramsPerSec <= 420) {
                                return qsTr("KEEPALIVE OK")
                            }
                            if (Stream.txDatagramsPerSec === 0) {
                                return qsTr("starting...")
                            }
                            return qsTr("cadence off")
                        }
                        color: {
                            if (Stream.txDatagramsPerSec >= 340 &&
                                Stream.txDatagramsPerSec <= 420) {
                                return "#7fff7f"
                            }
                            if (Stream.txDatagramsPerSec === 0) {
                                return "#ffd07f"
                            }
                            return "#ff7f7f"
                        }
                        font.family: "Consolas"
                        font.bold: true
                    }
                }
            }
        }

        // ---- main split: found radios (left) + log (right) ----
        SplitView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal

            // Left: found radios
            ColumnLayout {
                SplitView.preferredWidth: 460
                SplitView.minimumWidth: 320
                spacing: 6

                Label {
                    text: qsTr("Found radios")
                    font.bold: true
                    Layout.margins: 8
                }
                ListView {
                    id: radioList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.margins: 8
                    clip: true
                    model: ListModel { id: radioModel }
                    delegate: Rectangle {
                        width: radioList.width
                        height: 110
                        color: index % 2 ? "#1a1a1a" : "#222"
                        border.color: Stream.running &&
                                      Stream.targetIp === ip
                                      ? "#7fff7f" : "#333"
                        border.width: Stream.running &&
                                      Stream.targetIp === ip ? 2 : 1
                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 8
                            spacing: 8
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                Label {
                                    text: ip + "  —  " + boardName +
                                          (busy ? "  [BUSY]" : "")
                                    font.bold: true
                                    color: busy ? "#ff8866" : "#88ff88"
                                }
                                Label {
                                    text: qsTr("MAC: ") + mac
                                    color: "#bbb"
                                    font.pixelSize: 12
                                }
                                Label {
                                    text: qsTr("gateware: v") +
                                          codeVersion + "." +
                                          betaVersion +
                                          qsTr("   receivers: ") +
                                          numRxs
                                    color: "#bbb"
                                    font.pixelSize: 12
                                }
                            }
                            Button {
                                id: openBtn
                                Layout.preferredWidth: 90
                                Layout.alignment: Qt.AlignVCenter
                                text: {
                                    if (Stream.running &&
                                        Stream.targetIp === ip) {
                                        return qsTr("Close")
                                    }
                                    return qsTr("Open")
                                }
                                // Can open this row when nothing is
                                // streaming, OR close this row when
                                // it is the one currently streaming.
                                enabled: !Stream.running ||
                                         Stream.targetIp === ip
                                onClicked: {
                                    if (Stream.running &&
                                        Stream.targetIp === ip) {
                                        Stream.close()
                                    } else {
                                        Stream.open(ip)
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Right: live log (discovery + stream lines)
            ColumnLayout {
                SplitView.fillWidth: true
                spacing: 6
                Label {
                    text: qsTr("Log")
                    font.bold: true
                    Layout.margins: 8
                }
                ScrollView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.margins: 8
                    TextArea {
                        id: logArea
                        readOnly: true
                        font.family: "Consolas"
                        font.pixelSize: 12
                        color: "#ddd"
                        background: Rectangle { color: "#111" }
                        wrapMode: TextArea.WrapAnywhere
                    }
                }
            }
        }
    }

    // ---- wire C++ signals into the QML model -----------------
    Connections {
        target: Discovery
        function onRadioFound(ip, mac, boardName, codeVersion,
                              betaVersion, busy, numRxs) {
            radioModel.append({
                ip: ip, mac: mac, boardName: boardName,
                codeVersion: codeVersion, betaVersion: betaVersion,
                busy: busy, numRxs: numRxs
            })
        }
        function onScanFinished(count) {
            statusLabel.text = qsTr("Scan complete — ") + count +
                               qsTr(" radio(s) found.")
        }
        function onLogLine(line) {
            logArea.text += "[disc] " + line + "\n"
        }
    }

    // Stream worker on its own OS thread reports back here.
    Connections {
        target: Stream
        function onLogLine(line) {
            logArea.text += "[strm] " + line + "\n"
        }
    }

    // WDSP DLL loader reports here (Step 3a).
    Connections {
        target: Wdsp
        function onLogLine(line) {
            logArea.text += line + "\n"
        }
    }

    // WDSP RX channel engine reports here (Step 3c-ii).
    Connections {
        target: WdspEngine
        function onLogLine(line) {
            logArea.text += line + "\n"
        }
    }
}
