import QtQuick
import QtQuick.Controls

ApplicationWindow {
    visible: true
    width: 1600
    height: 900
    title: "Qt inspection probe"
    Component.onCompleted: console.log("Inspection probe ready")
    Button {
        x: 20
        y: 20
        width: 200
        height: 40
        text: "Block main loop"
        onClicked: {
            console.log("Main loop blocked")
            const deadline = Date.now() + 3000
            while (Date.now() < deadline) {}
            console.log("Main loop resumed")
        }
    }
    Column {
        y: 80
        Repeater {
            model: 800
            Button { text: "Control " + index }
        }
    }
}
