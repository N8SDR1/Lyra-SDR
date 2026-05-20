// Lyra — Step 2a QML shell.
//
// Discovery (Step 1) + Stream open/close + live EP6 stats (Step 2a).
// Click "Discover HL2" to scan the LAN; click "Open" on a found radio
// to start its EP6 stream on a dedicated OS thread; the banner shows
// live datagrams/sec + total + dropouts + framing errors.  Click
// "Close" to stop.  No DSP, no audio, no panadapter yet — Step 2a is
// the wire-path-only proof.

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: root
    width: 1000
    height: 680
    visible: true
    title: qsTr("Lyra — Hermes Lite 2 / 2+ — v0.0.2 (C++23 / Qt 6)")

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
                    radioModel.clear()
                    logArea.text = ""
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
        // open.  Updates at 5 Hz from the worker-thread atomics.
        Rectangle {
            id: streamBanner
            Layout.fillWidth: true
            implicitHeight: visible ? 40 : 0
            visible: Stream.running
            color: "#0a2a0a"
            border.color: "#2a6a2a"
            border.width: 1
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                spacing: 18

                Label {
                    text: qsTr("● STREAMING  ") + Stream.targetIp
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
                    text: Stream.dropouts + qsTr(" drop")
                    color: Stream.dropouts > 0 ? "#ff7f7f" : "#888"
                    font.family: "Consolas"
                }
                Label {
                    text: Stream.framingErrors + qsTr(" framing")
                    color: Stream.framingErrors > 0
                           ? "#ff7f7f" : "#888"
                    font.family: "Consolas"
                }
                Item { Layout.fillWidth: true }
                Label {
                    // ~5052 dg/s is the expected healthy rate at
                    // 48 kHz × 19 slots × 2 USB frames per UDP
                    // datagram.  Show "OK" once we're inside ±5 %.
                    text: {
                        if (Stream.datagramsPerSec >= 4800 &&
                            Stream.datagramsPerSec <= 5300) {
                            return qsTr("WIRE OK")
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
                        if (Stream.datagramsPerSec === 0) {
                            return "#ffd07f"
                        }
                        return "#ff7f7f"
                    }
                    font.family: "Consolas"
                    font.bold: true
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
}
