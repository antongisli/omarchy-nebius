import QtQuick
import QtQuick.Window
import Quickshell
import "plugin/qml/v076" as Plugin

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
  function findItem(parent, name) {
    if (parent.objectName === name) return parent
    for (var i = 0; i < parent.children.length; ++i) {
      var found = findItem(parent.children[i], name)
      if (found) return found
    }
    return null
  }
  Timer {
    interval: 150
    running: true
    onTriggered: {
      try {
        widget.clockMs = Date.now()
        var now = new Date(widget.clockMs).toISOString()
        test.require(!widget.countVisible && widget.countLabel === "", "Fresh install must not show a count or question mark")
        test.require(widget.barTooltip.indexOf("Set up Nebius") >= 0, "Fresh install tooltip must explain setup")
        var badge = test.findItem(widget, "vmCountBadge")
        test.require(badge && !badge.visible, "Actual badge item must be hidden before setup")
        var iconOnlyWidth = widget.implicitWidth
        var iconOnlyHeight = widget.implicitHeight
        test.barStub.vertical = true
        test.require(widget.implicitWidth === test.barStub.barSize, "Fresh vertical icon must keep the bar width")
        var verticalIconHeight = widget.implicitHeight
        test.barStub.vertical = false
        widget.snapshot = {ready: true, setup_complete: true, operation: {}, account: {}, capacity: {offerings: []}}
        test.require(widget.actions[2].key === "P" && widget.actions[2].title === "SSH port forwarding",
          "SSH ports must be a prominent launcher action")
        widget.vmCount = {count: 0, state: "current", checked_at: now}
        test.require(widget.countLabel === "0", "Confirmed zero must display zero")
        test.require(widget.countVisible && widget.badgeLabel === "0", "Confirmed zero must be a visible badge")
        test.require(badge.visible, "Actual badge item must appear after setup")
        test.require(badge.x >= 0 && badge.y >= 0 && badge.x + badge.width <= badge.parent.width
          && badge.y + badge.height <= badge.parent.height, "Badge must fit inside the icon canvas")
        widget.vmCount = {count: 5, state: "current", checked_at: now}
        test.require(widget.countLabel === "5", "Running count must appear")
        test.require(widget.implicitWidth === iconOnlyWidth && widget.implicitHeight === iconOnlyHeight,
          "Badge must overlay the icon without shifting neighboring widgets")
        widget.vmCount = {count: 1000, state: "current", checked_at: now}
        test.require(widget.badgeLabel === "9+" && widget.implicitWidth === iconOnlyWidth,
          "Large counts must fit a compact badge")
        test.require(widget.barTooltip.indexOf("1000 running VMs") >= 0, "Tooltip must retain the exact count")
        widget.vmCount = {count: null, state: "unavailable"}
        test.require(widget.countLabel === "?", "Unknown must not display zero")
        widget.vmCount = {count: 5, state: "current", checked_at: "2000-01-01T00:00:00Z"}
        test.require(widget.countLabel === "?", "Stale data must not look current")
        test.barStub.vertical = true
        test.require(widget.implicitWidth === test.barStub.barSize, "Vertical bar must keep its width")
        test.require(widget.implicitHeight === verticalIconHeight, "Vertical badge must not add a second row")
        test.barStub.vertical = false
        widget.snapshot = {ready: true, operation: {}, account: {}, capacity: {offerings: []}}
        widget.vmCount = {count: 5, state: "current", checked_at: now}
        test.require(widget.actions.some(function(action) {
          return action.key === "U" && action.screen === "uninstall" && action.title === "Uninstall Nebius plugin"
        }),
          "Uninstall must remain keyboard-accessible")
        widget.cursorIndex = 6
        widget.snapshot = {ready: false, setup_complete: true, operation: {}, account: {detail: "Reconnect required"}, capacity: {offerings: []}}
        test.require(widget.countVisible && widget.countLabel === "?", "Expired auth must keep an unknown badge, even with a recent cached count")
        test.require(widget.barTooltip.indexOf("reconnect required") >= 0, "Expired auth must explain recovery")
        test.require(widget.cursorIndex === 0,
          "Expiring authentication must select Reconnect, never an unrelated action")
        widget.snapshot = {ready: false, setup_complete: false, operation: {}, account: {detail: "Reconnect required"}, capacity: {offerings: []}}
        test.require(!widget.countVisible && widget.badgeLabel === "", "Incomplete setup must stay badge-free even after partial authentication")
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
