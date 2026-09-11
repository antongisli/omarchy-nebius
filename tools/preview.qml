import QtQuick
import QtQuick.Window
import Quickshell
import "plugin/qml/v057" as Plugin

// Headless artwork: actual widget content, synthetic state, no cloud polling.
ShellRoot {
  id: capture
  property QtObject barStub: QtObject {
    property bool vertical: false
    property int barSize: 36
    property string position: "top"
    property string fontFamily: "monospace"
    property color foreground: "#EDF2F5"
    property color barForeground: "#EDF2F5"
    property color urgent: "#FF9494"
    property bool foregroundAnimationEnabled: false
    function registerClickTarget(item) {}
    function unregisterClickTarget(item) {}
    function hideTooltip(item) {}
  }
  Window {
    id: canvas
    width: 1280
    height: 880
    visible: true
    color: "#E0FF4F"
    Rectangle { anchors.fill: parent; color: "#E0FF4F" }
    Text {
      x: 40; y: 32
      text: "Need a bigger GPU?"
      color: "#052B42"
      font.family: "sans-serif"
      font.pixelSize: 62
      font.bold: true
    }
    Text {
      x: 42; y: 111
      text: "Find capacity. Launch a VM. Jump in."
      color: "#052B42"
      font.family: "sans-serif"
      font.pixelSize: 27
    }
    Rectangle {
      x: 24; y: 181; width: 1232; height: 646
      color: "#101820"
      Image {
        x: 8; y: 8; width: 800; height: 624
        source: "plugin/assets/terminal-preview.svg"
        fillMode: Image.PreserveAspectFit
      }
      Rectangle { x: 846; y: 26; width: 1; height: 594; color: "#35454E" }
      Plugin.Widget {
        id: widget
        x: 1135; y: 22
        pollingEnabled: false
        bar: capture.barStub
        cursorActive: true
        cursorIndex: 0
        snapshot: ({ready:true, operation:{message:"No operations in progress"}, account:{},
          capacity:{updated_at:new Date().toISOString(), offerings:[{gpu_label:"H100"},{gpu_label:"H200"},{gpu_label:"RTX 6000 Ada"}]}})
        vmCount: ({count:2, state:"current", checked_at:new Date().toISOString()})
      }
    }
    Text {
      x: 40; y: 845
      text: "NEBIUS GPU  /  OMARCHY                              REAL INTERFACE · EXAMPLE DATA"
      color: "#052B42"
      font.family: "monospace"
      font.pixelSize: 17
    }
  }
  Timer {
    interval: 1300
    running: true
    onTriggered: canvas.contentItem.grabToImage(function(result) {
      result.saveToFile(Quickshell.env("NEBIUS_QA_IMAGE"))
      console.log("PREVIEW_OK")
      Qt.quit()
    })
  }
}
