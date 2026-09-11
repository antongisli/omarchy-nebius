import QtQuick

// The unopened Wayland popup has no offscreen backend. This QA-only stand-in
// lets the actual native bar button render without connecting to a compositor.
Item {
  required property Item anchorItem
  required property QtObject bar
  property var owner: null
  property bool open: false
  property Item focusTarget
  property int contentWidth: 360
  property int contentHeight: 570
  visible: false
  width: contentWidth
  height: contentHeight
  function fittedContentWidth(value) { return value }
  function fittedContentHeight(value, maximum) { return Math.min(value, maximum) }
}
