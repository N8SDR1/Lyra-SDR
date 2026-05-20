// Lyra — Step 1 minimal QML shell.
//
// Goal: prove the toolchain end-to-end.  Window opens, a "Discover
// HL2" button kicks the C++ worker thread, replies stream into a
// list, log lines stream into a scrollable text area.  No real UI
// yet — that lands AFTER the operator confirms discovery works.

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: root
    width: 900
    height: 600
    visible: true
    title: qsTr("Lyra — Hermes Lite 2 / 2+ — v0.0.1 (C++23 / Qt 6)")

    // ---- top bar ----------------------------------------------
    header: ToolBar {
        RowLayout {
            anchors.fill: parent
            anchors.margins: 8
            spacing: 12

            Button {
                id: scanBtn
                text: qsTr("Discover HL2")
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

    // ---- main split: found radios (left) + log (right) -------
    SplitView {
        anchors.fill: parent
        orientation: Qt.Horizontal

        // Left: found radios
        ColumnLayout {
            SplitView.preferredWidth: 420
            SplitView.minimumWidth: 260
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
                    height: 80
                    color: index % 2 ? "#1a1a1a" : "#222"
                    border.color: "#333"
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 8
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
                            text: qsTr("gateware: v") + codeVersion + "." +
                                  betaVersion +
                                  qsTr("   receivers: ") + numRxs
                            color: "#bbb"
                            font.pixelSize: 12
                        }
                    }
                }
            }
        }

        // Right: live discovery log
        ColumnLayout {
            SplitView.fillWidth: true
            spacing: 6
            Label {
                text: qsTr("Discovery log")
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
            logArea.text += line + "\n"
        }
    }
}
