import QtQuick
import QtQuick.Controls

ApplicationWindow {
    visible: true
    width: 1280
    height: 720
    title: "Qt menu probe"
    Component.onCompleted: console.log("Menu probe ready")
    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.RightButton
        onClicked: menu.popup()
    }
    Menu {
        id: menu
        popupType: Popup.Window
        onOpened: console.log("Menu opened")
        onClosed: console.log("Menu closed")
        MenuItem {
            text: "Choose this item"
            onTriggered: console.log("Item chosen")
        }
    }
}
