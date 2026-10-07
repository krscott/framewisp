import QtQuick
import QtQuick.Controls

ApplicationWindow {
    id: mainWindow
    visible: true
    width: 800
    height: 600
    title: "Coordinates main window"
    property bool mainPainted: false
    property bool dialogPainted: false
    readonly property bool probeReady: mainPainted && dialogPainted
    onFrameSwapped: mainPainted = true
    onProbeReadyChanged: {
        if (probeReady)
            console.log("Coordinates probe ready")
    }
    Window {
        id: dialog
        visible: true
        width: 480
        height: 240
        title: "Coordinates dialog"
        flags: Qt.Dialog
        modality: Qt.ApplicationModal
        Button {
            x: 300
            y: 150
            text: "Activate dialog"
            onClicked: console.log("Dialog activated")
        }
        onFrameSwapped: mainWindow.dialogPainted = true
    }
}
