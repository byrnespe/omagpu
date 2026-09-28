import QtQuick
import QtQuick.Controls
import qs.Commons

Button {
  id: control
  property bool accent: false
  implicitHeight: Style.space(34)
  implicitWidth: label.implicitWidth + Style.space(24)
  hoverEnabled: true
  opacity: enabled ? 1 : 0.4
  contentItem: Text {
    id: label
    text: control.text
    textFormat: Text.PlainText
    color: control.accent ? Color.accent : Color.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    font.bold: control.accent
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
  }
  background: Rectangle {
    radius: Style.cornerRadius
    color: control.down ? Qt.rgba(1, 1, 1, 0.14) : control.hovered ? Qt.rgba(1, 1, 1, 0.08) : "transparent"
    border.width: 1
    border.color: control.activeFocus || control.accent ? Color.accent : Qt.rgba(1, 1, 1, 0.22)
  }
}
