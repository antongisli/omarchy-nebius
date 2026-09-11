import QtQuick

// Capture-only popup host. Production content and controls are unchanged;
// offscreen Qt cannot create Omarchy's Wayland popup surface.
Rectangle {
  required property Item anchorItem
  required property QtObject bar
  property var owner: null
  property bool open: false
  property Item focusTarget
  property int contentWidth: 360
  property int contentHeight: 570
  visible: true
  color: "#101820"
  x: -282
  y: 62
  width: contentWidth
  height: contentHeight
  function fittedContentWidth(value) { return value }
  function fittedContentHeight(value, maximum) { return Math.min(value, maximum) }
}
