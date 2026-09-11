import QtQuick
import QtQuick.Window
import Quickshell
import "plugin/qml/v057" as Plugin

// Run only with QT_QPA_PLATFORM=offscreen. No cloud polling or desktop window.
ShellRoot {
  id: test
  property QtObject barStub: QtObject {
    property bool vertical: false
    property int barSize: 34
    property string position: "top"
    property string fontFamily: "monospace"
    property color foreground: "#eeeeee"
    property color barForeground: "#eeeeee"
    property color urgent: "#ff6666"
    property bool foregroundAnimationEnabled: false
    function registerClickTarget(item) {}
    function unregisterClickTarget(item) {}
    function hideTooltip(item) {}
  }
  Window {
    id: window
    width: 320
    height: 80
    visible: true
    color: "#181818"
    Plugin.Widget {
      id: widget
      anchors.centerIn: parent
      pollingEnabled: false
      bar: test.barStub
    }
  }
  function require(condition, message) {
    if (!condition) throw new Error(message)
  }
  Timer {
    interval: 150
    running: true
    onTriggered: {
      try {
        widget.clockMs = Date.now()
        var now = new Date(widget.clockMs).toISOString()
        widget.vmCount = {count: 0, state: "current", checked_at: now}
        test.require(widget.countLabel === "0", "Confirmed zero must display zero")
        widget.vmCount = {count: 5, state: "current", checked_at: now}
        test.require(widget.countLabel === "5", "Running count must appear")
        var smallWidth = widget.implicitWidth
        widget.vmCount = {count: 1000, state: "current", checked_at: now}
        test.require(widget.implicitWidth > smallWidth, "Multi-digit count must expand its native button")
        widget.vmCount = {count: null, state: "unavailable"}
        test.require(widget.countLabel === "?", "Unknown must not display zero")
        widget.vmCount = {count: 5, state: "current", checked_at: "2000-01-01T00:00:00Z"}
        test.require(widget.countLabel === "?", "Stale data must not look current")
        test.barStub.vertical = true
        test.require(widget.implicitWidth === test.barStub.barSize, "Vertical bar must keep its width")
        test.barStub.vertical = false
        widget.snapshot = {ready: true, operation: {}, account: {}, capacity: {offerings: []}}
        widget.vmCount = {count: 5, state: "current", checked_at: now}
        test.require(widget.actions.some(function(action) {
          return action.key === "U" && action.screen === "uninstall" && action.title === "Uninstall Nebius plugin"
        }),
          "Uninstall must remain keyboard-accessible")
        widget.cursorIndex = 6
        widget.snapshot = {ready: false, operation: {}, account: {detail: "Reconnect required"}, capacity: {offerings: []}}
        test.require(widget.cursorIndex === 0,
          "Expiring authentication must select Reconnect, never an unrelated action")
        widget.snapshot = {ready: true, operation: {}, account: {}, capacity: {offerings: []}}
        widget.cursorIndex = 0
        Qt.callLater(function() {
          window.contentItem.grabToImage(function(result) {
            var path = Quickshell.env("NEBIUS_QA_IMAGE")
            if (path) result.saveToFile(path)
            console.log("WIDGET_QA_OK count=5 width=" + widget.implicitWidth)
            Qt.quit()
          })
        })
      } catch (error) {
        console.error("WIDGET_QA_FAILED " + error)
        Qt.exit(1)
      }
    }
  }
}
