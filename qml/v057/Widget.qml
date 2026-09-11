import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Omarchy-native widget: running VM count, local uninstall, and compact actions.
// Full tasks run in a tiled terminal with persistent keyboard hints and progress.
Panel {
  id: root
  moduleName: "nebius"
  ipcTarget: "nebius"
  manageIpc: false
  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  readonly property string pluginRoot: Quickshell.env("HOME") + "/.config/omarchy/plugins/nebius"
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  // Nebius brand accents; panel surfaces and typography still follow Omarchy.
  readonly property color accent: "#E0FF4F"
  readonly property color brandInk: "#052B42"
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  property var snapshot: ({ ready: false, account: {}, operation: {}, capacity: { projects: [], offerings: [] } })
  property int cursorIndex: 0
  property bool cursorActive: false
  property var vmCount: ({ count: null, state: "unavailable", detail: "Checking running VMs" })
  property double lastCountRequestMs: 0
  property double clockMs: Date.now()
  property bool pollingEnabled: true
  readonly property bool countVisible: ready || snapshot.setup_complete === true
  readonly property bool countCurrent: ready && vmCount.state === "current" && typeof vmCount.count === "number"
    && vmCount.count >= 0 && isFinite(Date.parse(vmCount.checked_at)) && clockMs - Date.parse(vmCount.checked_at) < 90000
  readonly property string countLabel: !countVisible ? "" : countCurrent ? String(vmCount.count) : "?"
  readonly property string badgeLabel: countCurrent && vmCount.count > 9 ? "9+" : countLabel
  readonly property string countSummary: !countVisible ? "Set up Nebius" : countCurrent ? vmCount.count + " running VM" + (vmCount.count === 1 ? "" : "s") : "Running VM count unavailable"
  readonly property bool ready: snapshot.ready === true
  readonly property bool busy: snapshot.operation && snapshot.operation.phase === "running"
  readonly property bool needsReconnect: snapshot.account && snapshot.account.detail === "Reconnect required"
  readonly property string barTooltip: !countVisible
    ? "Nebius · Set up Nebius\nOpen the panel and press S to connect your account."
    : (needsReconnect ? "Nebius session expired — reconnect required\n" : "Nebius · ")
      + countSummary + "\nRunning VMs in your visible personal projects"
      + (countCurrent ? " · checked within 90s" : "\n" + (vmCount.detail || "Refresh needed"))
      + (busy ? "\nOperation in progress" : "")
  readonly property var actions: ready ? [
    { key: "G", title: "Get a GPU VM", screen: "get" },
    { key: "J", title: "Jump into a VM", screen: "jump" },
    { key: "V", title: "Your VMs", screen: "overview" },
    { key: "C", title: "GPU capacity", screen: "capacity" },
    { key: "A", title: busy ? "Follow progress" : "Last operation", screen: "activity" },
    { key: "S", title: "Account / reconnect", screen: "setup" },
    { key: "U", title: "Uninstall Nebius plugin", screen: "uninstall" }
  ] : [
    { key: "S", title: needsReconnect ? "Reconnect account" : "Set up Nebius", screen: "setup" },
    { key: "A", title: "Last operation", screen: "activity" },
    { key: "U", title: "Uninstall Nebius plugin", screen: "uninstall" }
  ]

  onActionsChanged: {
    cursorIndex = 0
    Qt.callLater(function() { scroller.contentY = 0 })
  }

  function activateCurrent() {
    var action = actions[cursorIndex]
    if (action) launch(action.screen)
  }

  function refresh(forceCount) {
    clockMs = Date.now()
    if (ready && !countProc.running && (forceCount === true || clockMs - lastCountRequestMs >= 30000)) {
      lastCountRequestMs = clockMs
      countProc.running = true
    }
    if (statusProc.running) return
    statusProc.command = [root.pluginRoot + "/bin/nebius-status", "--json"]
    statusProc.running = true
  }
  onReadyChanged: if (ready && pollingEnabled) refresh(true)
  function launch(screen) {
    root.close()
    if (screen === "setup") {
      Quickshell.execDetached(["omarchy-launch-tui", "--app-id=org.nebius.setup", root.pluginRoot + "/bin/nebius-setup"])
    } else if (screen === "uninstall") {
      Quickshell.execDetached(["omarchy-launch-tui", "--app-id=org.nebius.uninstall", root.pluginRoot + "/bin/nebius-uninstall"])
    } else {
      Quickshell.execDetached(["omarchy-launch-tui", "--app-id=org.nebius.manager", root.pluginRoot + "/bin/nebius-ui", screen])
    }
  }
  function moveCursor(delta) {
    cursorActive = true
    cursorIndex = (cursorIndex + delta + actions.length) % actions.length
    Qt.callLater(function() {
      var item = actionRepeater.itemAt(root.cursorIndex)
      if (!item) return
      if (item.y < scroller.contentY) scroller.contentY = item.y
      else if (item.y + item.height > scroller.contentY + scroller.height)
        scroller.contentY = Math.min(scroller.contentHeight - scroller.height, item.y + item.height - scroller.height)
    })
  }
  function activityText() {
    var op = snapshot.operation || {}
    var message = String(op.message || "No operations yet")
    if (message.toLowerCase().indexOf("quota") >= 0) return "Quota blocked the last launch. A opens details."
    return message.split("\n")[0]
  }
  function snapshotText() {
    var cap = snapshot.capacity || {}
    var names = ({})
    var rows = cap.offerings || []
    for (var i = 0; i < rows.length; i++) names[rows[i].gpu_label] = true
    var count = Object.keys(names).length
    var age = cap.updated_at ? Math.max(0, Math.floor((Date.now() - Date.parse(cap.updated_at)) / 60000)) : 0
    return count ? count + " GPU types · snapshot " + age + " min old" : "Explore regional GPU capacity"
  }
  onOpenedChanged: if (opened) {
    cursorIndex = 0
    cursorActive = true
    if (pollingEnabled) refresh(true)
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Process {
    id: countProc
    command: ["timeout", "90s", "python3", root.pluginRoot + "/libexec/nebius_core.py", "vm-count", "--refresh"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.clockMs = Date.now()
        try { root.vmCount = JSON.parse(text) }
        catch (error) { root.vmCount = { count: null, state: "unavailable", detail: "VM refresh failed. Open Your VMs for details." } }
      }
    }
  }
  Process {
    id: statusProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.snapshot = JSON.parse(text) }
        catch (error) { root.snapshot = { ready: false, setup_complete: root.countVisible, account: {}, operation: {message: "Status unavailable. Open account setup."} } }
      }
    }
  }
  Timer {
    interval: root.opened || root.busy ? 1500 : 30000
    running: root.pollingEnabled
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }
  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(true); return "ok" }
    function status(): string { return JSON.stringify(Object.assign({}, root.snapshot, {vms: root.vmCount, vm_count_label: root.countLabel, vm_count_visible: root.countVisible, vm_badge_label: root.badgeLabel})) }
    function setup(): string { root.launch("setup"); return "ok" }
    function gpu(): string { root.launch("get"); return "ok" }
    function jump(): string { root.launch("jump"); return "ok" }
    function vms(): string { root.launch("overview"); return "ok" }
    function capacity(): string { root.launch("capacity"); return "ok" }
    function uninstall(): string { root.launch("uninstall"); return "ok" }
  }
  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    active: root.ready || root.busy
    activeColor: root.accent
    foreground: root.foreground
    fontFamily: root.fontFamily
    text: root.countVisible ? "Nebius " + root.countLabel : "Nebius"
    useActiveColor: false
    slotSize: Math.max(Style.bar.iconSlot, Style.space(28))
    opticalSize: Style.space(26)
    iconComponent: Component {
      Item {
        Image {
          anchors.centerIn: parent
          source: "../../assets/nebius-icon.svg"
          width: Style.space(22)
          height: width
          sourceSize.width: width * 2
          sourceSize.height: height * 2
        }
        Rectangle {
          objectName: "vmCountBadge"
          visible: root.countVisible
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          width: Style.space(14)
          height: width
          radius: width / 2
          color: root.accent
          border.width: 1
          border.color: root.brandInk
          Text {
            anchors.centerIn: parent
            text: root.badgeLabel
            color: root.brandInk
            font.family: root.fontFamily
            font.pixelSize: Style.space(9)
            font.bold: true
            renderType: Text.NativeRendering
          }
        }
      }
    }
    tooltipText: root.barTooltip
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) root.launch("jump")
      else root.toggle()
    }
  }
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight, Style.space(570))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      clip: true
      onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveCursor(dy) }
      onActivateRequested: root.activateCurrent()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(value) {
        if (value.toUpperCase() === "R") { root.refresh(true); return }
        for (var i = 0; i < root.actions.length; i++) {
          if (root.actions[i].key === value.toUpperCase()) { root.launch(root.actions[i].screen); return }
        }
      }
      Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        Column {
          id: contentColumn
          width: parent.width
          spacing: Style.space(12)
          RowLayout {
            width: parent.width
            Image {
              source: "../../assets/nebius-logo.svg"
              Layout.preferredWidth: Style.space(131)
              Layout.preferredHeight: Style.space(36)
              fillMode: Image.PreserveAspectFit
            }
            Item { Layout.fillWidth: true }
            Text {
              text: root.busy ? "WORKING" : root.ready ? "CONNECTED" : root.needsReconnect ? "RECONNECT" : "SETUP"
              color: root.needsReconnect ? root.urgent : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
          Text {
            width: parent.width
            text: root.needsReconnect
              ? "Nebius session expired — reconnect required.\nPress S or Enter to reconnect."
              : root.ready ? root.countSummary + "\n" + root.snapshotText()
              : "Need a bigger GPU?\nConnect Nebius to find capacity, launch a VM, and jump in."
            wrapMode: Text.WordWrap
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }
          PanelSeparator { width: parent.width; foreground: root.foreground }
          Repeater {
            id: actionRepeater
            model: root.actions
            Button {
              required property var modelData
              required property int index
              width: parent.width
              text: modelData.key + "  " + modelData.title
              readonly property bool highlighted: root.cursorActive && root.cursorIndex === index
              foreground: highlighted ? root.brandInk : root.foreground
              accent: root.accent
              color: highlighted ? root.accent : "transparent"
              fontFamily: root.fontFamily
              leftAlign: true
              bordered: index === 0
              hasCursor: root.cursorActive && root.cursorIndex === index
              onHovered: function(value) { if (value) { root.cursorActive = true; root.cursorIndex = index } }
              onClicked: root.launch(modelData.screen)
            }
          }
          PanelSeparator { width: parent.width; foreground: root.foreground }
          Text {
            width: parent.width
            text: root.activityText()
            wrapMode: Text.WrapAnywhere
            maximumLineCount: 3
            elide: Text.ElideRight
            color: root.snapshot.operation && root.snapshot.operation.phase === "error" ? root.urgent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
          Text {
            width: parent.width
            text: "Enter opens · R refreshes status · Esc closes"
            wrapMode: Text.WordWrap
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }
}
