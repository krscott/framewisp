import QtQuick
import QtQuick.Controls

ApplicationWindow {
    visible: true
    width: 800
    height: 600
    title: "Coordinates main window"
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
        Component.onCompleted: console.log("Coordinates probe ready")
    }
}
