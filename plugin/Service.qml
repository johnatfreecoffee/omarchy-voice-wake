// Voice Wake overlay. The oma-voice-wake listener drives it over the shell IPC target "voicewake":
//   show '{"name":"Jack"}'   black full-screen overlay with the head in a white circle (click-through)
//   setCapture true|false    hide it for one screenshot while the listener finds Grok Bot's voice button
//   setLive true             from now on a mouse move or any key dismisses it
//   hide / isVisible
// The head bobs and the rings pulse with the microphone level the listener writes to
// $XDG_RUNTIME_DIR/oma-voice-wake/level while the overlay is up.
import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons

Item {
  id: root

  property var shell: null
  property var manifest: null
  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")).replace(/\/$/, "")
  readonly property string runDir: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/oma-voice-wake"
  readonly property string headPath: root.pluginDir + "/head.png"

  property bool shown: false
  property bool live: false
  property bool capturing: false
  property string agentName: ""
  property real level: 0
  property double liveAt: 0

  function showOverlay(payload) {
    var name = ""
    try { name = (JSON.parse(payload || "{}").name || "") } catch (e) { name = "" }
    root.agentName = name
    root.live = false
    root.capturing = false
    root.level = 0
    root.shown = true
    safetyTimer.restart()
  }

  function dismiss() {
    root.shown = false
    root.live = false
    root.capturing = false
  }

  ShellIpc {
    target: "voicewake"

    function show(payload: string): string { root.showOverlay(payload); return "ok" }
    function hide(): string { root.dismiss(); return "ok" }
    function setLive(on: string): string {
      root.live = (on === "true" || on === "1")
      root.liveAt = Date.now()
      return "ok"
    }
    function setCapture(on: string): string { root.capturing = (on === "true" || on === "1"); return "ok" }
    function isVisible(): string { return root.shown ? "true" : "false" }
  }

  // Never leave the screen black: give up after ten minutes regardless.
  Timer { id: safetyTimer; interval: 600000; onTriggered: root.dismiss() }

  FileView { id: levelFile; path: root.runDir + "/level"; printErrors: false; blockLoading: true }
  Timer {
    interval: 80
    repeat: true
    running: root.shown && !root.capturing
    onTriggered: {
      levelFile.reload()
      var v = parseFloat(levelFile.text() || "0")
      if (isNaN(v)) v = 0
      v = Math.min(1, Math.max(0, (v - 0.015) * 4))
      root.level = root.level * 0.6 + v * 0.4
    }
  }

  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: win
      required property var modelData
      screen: modelData
      visible: root.shown && !root.capturing
      color: "#000000"
      anchors { top: true; bottom: true; left: true; right: true }
      exclusionMode: ExclusionMode.Ignore
      WlrLayershell.namespace: "oma-voice-wake"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: root.live ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
      // Click-through until live, so the listener can press Grok Bot's voice button underneath.
      mask: root.live ? null : noInput
      Region { id: noInput }

      Item {
        id: content
        anchors.fill: parent
        focus: true
        Keys.onPressed: function(event) { if (root.live) { root.dismiss(); event.accepted = true } }

        property real lastX: -1
        property real lastY: -1
        MouseArea {
          anchors.fill: parent
          hoverEnabled: true
          acceptedButtons: Qt.AllButtons
          onPositionChanged: function(mouse) {
            if (!root.live || Date.now() - root.liveAt < 600) { content.lastX = mouse.x; content.lastY = mouse.y; return }
            if (content.lastX < 0) { content.lastX = mouse.x; content.lastY = mouse.y; return }
            if (Math.abs(mouse.x - content.lastX) + Math.abs(mouse.y - content.lastY) > 6) root.dismiss()
          }
          onPressed: if (root.live) root.dismiss()
          onWheel: function(w) { if (root.live) root.dismiss() }
        }

        // Pulse rings, scaled by the voice level.
        Repeater {
          model: 3
          Rectangle {
            required property int index
            anchors.centerIn: disc
            width: disc.width * (1.12 + index * 0.16 + root.level * (0.35 + index * 0.22))
            height: width
            radius: width / 2
            color: "transparent"
            border.width: 2
            border.color: Qt.rgba(1, 1, 1, Math.max(0.05, 0.32 - index * 0.09 + root.level * 0.25))
            Behavior on width { NumberAnimation { duration: 90 } }
          }
        }

        Rectangle {
          id: disc
          anchors.centerIn: parent
          anchors.verticalCenterOffset: -30
          width: Math.min(parent.width, parent.height) * 0.26
          height: width
          radius: width / 2
          color: "#ffffff"
          scale: 1 + root.level * 0.05

          // The Grok Bot app icon, inverted and cut to a circle (head.png): the head rocks and
          // breathes inside the white disc, more as the voice level rises.
          Image {
            id: head
            anchors.centerIn: parent
            width: parent.width * 0.92
            height: width
            source: "file://" + root.headPath + "?v=2"
            sourceSize.width: 512
            sourceSize.height: 512
            fillMode: Image.PreserveAspectFit
            smooth: true
            mipmap: true
            property real phase: 0
            NumberAnimation on phase { from: 0; to: Math.PI * 2; duration: 900; loops: Animation.Infinite; running: root.shown }
            rotation: Math.sin(head.phase) * (1.5 + root.level * 8)
            scale: 1 + Math.sin(head.phase * 2) * (0.008 + root.level * 0.03)
          }
        }

        Text {
          anchors.horizontalCenter: parent.horizontalCenter
          anchors.top: disc.bottom
          anchors.topMargin: disc.width * 0.42
          textFormat: Text.PlainText
          text: root.agentName.length ? root.agentName : "Grok Bot"
          color: "#ffffff"
          font.pixelSize: 26
          font.weight: Font.DemiBold
        }
        Text {
          anchors.horizontalCenter: parent.horizontalCenter
          anchors.bottom: parent.bottom
          anchors.bottomMargin: 36
          textFormat: Text.PlainText
          text: root.live ? "Move the mouse or press any key to see Grok Bot" : "Starting voice…"
          color: Qt.rgba(1, 1, 1, 0.45)
          font.pixelSize: 13
        }
      }
    }
  }
}
