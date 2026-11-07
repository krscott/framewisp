// Shows text and controls that change with every setting framewisp sanitizes.
import QtQuick
import QtQuick.Controls

ApplicationWindow {
    visible: true
    title: "Environment probe"
    // A fixed size makes Sway float the window, so its title bar shows too.
    minimumWidth: 640
    maximumWidth: 640
    minimumHeight: 400
    maximumHeight: 400

    Column {
        padding: 12
        spacing: 8

        Label {
            text: "Default font café 日本語"
        }
        Label {
            font.family: "sans-serif"
            text: "Sans-serif family"
        }
        Label {
            font.family: "serif"
            text: "Serif family"
        }
        Label {
            font.family: "monospace"
            text: "Monospace family"
        }
        Label {
            text: Qt.locale().name + " " + Qt.locale().monthName(0)
        }
        Label {
            // A fixed instant shows only the time zone's offset.
            text: new Date(0).toString()
        }
        Button {
            text: "Button"
        }
        CheckBox {
            text: "Check"
        }
    }
}
