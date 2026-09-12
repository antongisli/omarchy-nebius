import QtQuick
import QtQuick.Window
import Quickshell

// Rasterize the production terminal drawing capture; no desktop or cloud access.
ShellRoot {
  Window {
    id: canvas
    visible: true
    width: terminal.implicitWidth || 800
    height: terminal.implicitHeight || 528
    color: "#101820"
    Image {
      id: terminal
      anchors.fill: parent
      source: Quickshell.env("NEBIUS_PREVIEW_SOURCE")
    }
  }
  Timer {
    interval: 500
    running: true
    onTriggered: {
      if (terminal.status !== Image.Ready) {
        console.error("TERMINAL_CAPTURE_FAILED: image not ready")
        Qt.exit(1)
        return
      }
      canvas.contentItem.grabToImage(function(result) {
        if (!result.saveToFile(Quickshell.env("NEBIUS_QA_IMAGE"))) {
          Qt.exit(1)
          return
        }
        console.log("TERMINAL_CAPTURE_OK")
        Qt.quit()
      })
    }
  }
}
