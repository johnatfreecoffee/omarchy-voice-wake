// Voice Wake overlay. The oma-voice-wake listener drives it over the shell IPC target "voicewake":
//   show '{"name":"Jack"}'   black full-screen overlay with the head in a white circle (click-through)
//   setCapture true|false    hide it for one screenshot while the listener finds Grok Bot's voice button
//   setLive true             voice chat is live (the overlay stays until the chat ends)
// A mouse move, click, wheel, Esc or any key dismisses it at every stage, including
// "Starting voice…". While not live it also gives up on its own after 25 s.
//   hide / isVisible
// The official Grok Bot icon is a cropped close-up of the head (chin and left side clipped).
// install.sh rebuilds head.png as the full head, centered, so it sits inside the disc with padding.
// The head rocks gently; a phone-call-style bar waveform under the disc follows the
// microphone level the listener writes to $XDG_RUNTIME_DIR/oma-voice-wake/level.
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
  // Input before this time is ignored (overlay just appeared, or a capture just ended and the
  // listener's virtual pointer moved underneath us).
  property double armedAt: 0

  function arm(ms) {
    root.armedAt = Date.now() + ms
    root.lastX = -1
    root.lastY = -1
  }
  property real lastX: -1
  property real lastY: -1

  function showOverlay(payload) {
    var name = ""
    try { name = (JSON.parse(payload || "{}").name || "") } catch (e) { name = "" }
    root.agentName = name
    root.live = false
    root.capturing = false
    root.level = 0
    root.shown = true
    root.arm(700)
    liveTimer.stop()
    startTimer.restart()
  }

  function dismiss() {
    startTimer.stop()
    liveTimer.stop()
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
      root.arm(600)
      if (root.live) { startTimer.stop(); liveTimer.restart() }
      return "ok"
    }
    function setCapture(on: string): string {
      var c = (on === "true" || on === "1")
      root.capturing = c
      if (!c) root.arm(500)
      return "ok"
    }
    function isVisible(): string { return root.shown ? "true" : "false" }
  }

  // Never leave the screen black: 25 s at most while voice is still starting, and ten
  // minutes at most once the voice chat is live.
  Timer { id: startTimer; interval: 25000; onTriggered: root.dismiss() }
  Timer { id: liveTimer; interval: 600000; onTriggered: root.dismiss() }

  function userInput() {
    if (!root.shown || root.capturing || Date.now() < root.armedAt) return false
    root.dismiss()
    return true
  }

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
      // Always takes the keyboard and pointer so Esc / any key / the mouse can dismiss it. The
      // listener hides it (setCapture) for the moment it screenshots and clicks Grok Bot.
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive

      Item {
        id: content
        anchors.fill: parent
        focus: true
        Keys.onPressed: function(event) { if (root.userInput()) event.accepted = true }

        MouseArea {
          anchors.fill: parent
          hoverEnabled: true
          acceptedButtons: Qt.AllButtons
          onPositionChanged: function(mouse) {
            if (Date.now() < root.armedAt || root.lastX < 0) { root.lastX = mouse.x; root.lastY = mouse.y; return }
            if (Math.abs(mouse.x - root.lastX) + Math.abs(mouse.y - root.lastY) > 6) root.userInput()
          }
          onPressed: root.userInput()
          onWheel: function(w) { root.userInput() }
        }

        Column {
          id: cluster
          anchors.centerIn: parent
          anchors.verticalCenterOffset: -12
          spacing: 0
          width: parent.width

          Rectangle {
            id: disc
            anchors.horizontalCenter: parent.horizontalCenter
            // Dominant on screen: about 46% of the height, capped so it still fits in width.
            width: Math.min(content.width * 0.72, content.height * 0.46)
            height: width
            radius: width / 2
            color: "#ffffff"
            antialiasing: true

            // Full reconstructed head, centered, with comfortable white padding.
            Image {
              id: head
              anchors.centerIn: parent
              width: parent.width * 0.80
              height: width
              source: "file://" + root.headPath + "?v=3"
              sourceSize.width: 1024
              sourceSize.height: 1024
              fillMode: Image.PreserveAspectFit
              smooth: true
              mipmap: true
              antialiasing: true
              property real phase: 0
              NumberAnimation on phase { from: 0; to: Math.PI * 2; duration: 900; loops: Animation.Infinite; running: root.shown }
              rotation: Math.sin(head.phase) * (1.5 + root.level * 8)
              scale: 1 + Math.sin(head.phase * 2) * (0.008 + root.level * 0.03)
            }
          }

          Item { width: 1; height: 28 }

          Text {
            id: agentLabel
            anchors.horizontalCenter: parent.horizontalCenter
            textFormat: Text.PlainText
            text: root.agentName.length ? root.agentName : "Grok Bot"
            color: "#ffffff"
            font.pixelSize: 34
            font.weight: Font.DemiBold
          }

          Item { width: 1; height: 22 }

          // In-call waveform: 32 rounded bars that grow from the middle, idle sine
          // floor plus the mic level, colored emerald → cyan → sky.
          Item {
            id: wave
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.min(content.width * 0.62, disc.width * 1.4)
            height: Math.max(80, disc.width * 0.26)
            property int barCount: 32
            property real gap: 5
            property real barW: Math.max(3, (width - gap * (barCount - 1)) / barCount)
            property real phase: 0
            NumberAnimation on phase {
              from: 0
              to: Math.PI * 2
              duration: 2800
              loops: Animation.Infinite
              running: root.shown && !root.capturing
            }

            Repeater {
              model: wave.barCount
              Rectangle {
                required property int index
                width: wave.barW
                radius: width / 2
                antialiasing: true
                x: index * (wave.barW + wave.gap)
                anchors.verticalCenter: parent.verticalCenter
                property real idle: 0.08 + 0.05 * Math.sin(wave.phase + index * 0.35)
                property real dome: 0.45 + 0.55 * Math.sin(Math.PI * (index + 0.5) / wave.barCount)
                property real jig: 0.88 + 0.12 * Math.sin(index * 1.7 + 0.4)
                property real travel: 0.78 + 0.22 * Math.sin(wave.phase * 1.4 - index * 0.22)
                property real t: Math.max(idle, idle + (1 - idle) * Math.min(1, root.level * 1.35) * dome * jig * travel)
                height: Math.max(width, t * wave.height * 0.95)
                color: {
                  var u = index / Math.max(1, wave.barCount - 1)
                  var r, g, b
                  if (u < 0.5) {
                    var s = u * 2
                    r = (16 + (34 - 16) * s) / 255
                    g = (185 + (211 - 185) * s) / 255
                    b = (129 + (238 - 129) * s) / 255
                  } else {
                    var s = (u - 0.5) * 2
                    r = (34 + (56 - 34) * s) / 255
                    g = (211 + (189 - 211) * s) / 255
                    b = (238 + (248 - 238) * s) / 255
                  }
                  return Qt.rgba(r, g, b, 0.96)
                }
                Behavior on height { NumberAnimation { duration: 80; easing.type: Easing.OutCubic } }
              }
            }
          }
        }

        Text {
          anchors.horizontalCenter: parent.horizontalCenter
          anchors.bottom: parent.bottom
          anchors.bottomMargin: 36
          textFormat: Text.PlainText
          text: root.live ? "Move the mouse or press any key to see Grok Bot" : "Starting voice…   Press Esc or move the mouse to cancel"
          color: Qt.rgba(1, 1, 1, 0.45)
          font.pixelSize: 13
        }
      }
    }
  }
}
