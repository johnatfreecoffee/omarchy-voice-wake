# omarchy-voice-wake

Say **"Hey &lt;agent name&gt;"** and talk to your own Grok Bot agent, on [Omarchy](https://omarchy.org) 4.5+
(Hyprland, Quickshell shell). Works on the desktop and from the lock screen. Listening is
**fully offline**: nothing is recorded or sent anywhere until the Grok Bot app opens.

- **Unlocked:** the screen goes black with the full Grok Bot head rocking inside a large white
  circle, and a green-to-blue bar waveform under it moves with your voice, like a phone-call
  visualizer. Grok Bot opens that agent (it will launch the app if it isn't already running,
  bring its window onto the visible workspace, even from Oma's Stage Manager strip) and starts
  voice chat. When the voice chat ends (the app closes its microphone), the black screen hides
  itself and focus goes back to Grok Bot. Esc, any key, a click or a mouse move clears it at
  any time, including while it still says "Starting voice…", and it never stays on "Starting
  voice…" longer than 25 seconds. If the chat has an unsent draft, Grok Bot hides its voice-chat
  button (the mic beside the send arrow is dictation), so nothing is clicked: the overlay hides
  and a notification asks you to send or clear the draft. If voice can't be confirmed, the
  overlay hides and a notification says to click the voice button to talk.
- **Locked** (with the optional [lock integration](lock-integration/README.md)): it asks
  *"What's your voice password?"*, checks what you say on-device, unlocks, then does the above.
  A mic button on the lock screen does the same check on click. Only on Wi-Fi networks you trust;
  everywhere else you type your password and the agent opens right after.
- Light enough for a 2015 MacBook Air: about **1% of one CPU core** while quiet (an energy gate keeps
  the recognizer asleep in silence), ~170 MB RAM (mostly the speech model).

## Requirements

Omarchy 4.5 (Quickshell shell, Hyprland 0.56), PipeWire, a microphone, the **Grok Bot desktop app**
(signed in), and: `uv grim gcc wayland pkgconf curl unzip imagemagick libnotify`.

## Install

```sh
git clone https://github.com/johnatfreecoffee/omarchy-voice-wake.git
cd omarchy-voice-wake && ./install.sh
```

No sudo. It installs into `~/.local/share/oma-voice-wake` (Python 3.12 venv with Vosk + Piper, the Vosk
small English model, an offline Piper voice for the prompts), builds `oma-vclick` (a tiny Wayland
virtual-pointer clicker; no root, no uinput), enables the `johnatfreecoffee.voice-wake` shell plugin
(the overlay) and starts `oma-voice-wake.service` (systemd --user). Every replaced file is backed up
as `<file>.bak-<epoch>`.

## Add your agents

Edit `~/.config/oma/voice-wake.json` (mode 600):

```json
"agents": [
  {"name": "Jack", "phrase": "hey jack", "agentId": "<grok bot agent id>", "enabled": true},
  {"name": "Ava",  "phrase": "hey ava",  "agentId": "<grok bot agent id>", "enabled": true}
]
```

The agent id is the `id=` in the agent's Grok Bot link (`grokbot://app/v1/agent?id=…`). Phrases
should be ordinary words the small English model knows. `oma-voice-wake test-wake` listens once and
shows what it heard. The listener reloads the file by itself.

## Voice unlock (optional)

1. Apply the [lock integration](lock-integration/README.md) to your clone of the lock plugin.
2. Set the passcode by typing it (twice, hidden): `oma-voice-wake passcode set`
3. Add trusted networks: `"voiceUnlock": {"enabled": true, "trustedWifi": ["My Home Wi-Fi"]}`
4. Lock once and unlock **by typing** (voice unlock is off after every boot until you do).

Read [SECURITY.md](SECURITY.md) first: a spoken word is weak security.

## How voice chat gets started (and why it's fragile)

Grok Bot has no link or shortcut that starts voice mode. The listener opens the agent with
`xdg-open grokbot://app/v1/agent?id=…`, waits for the window, asks Oma's Stage Manager to promote it
(or moves it out of `special:oma-stage`), focuses it and waits for its position to settle. Then it
hides the overlay, screenshots the window's real geometry from `hyprctl` with `grim` (saved to
`~/.local/state/oma-voice-wake/last-handoff.png`), and looks at the composer's right-hand button:
the "Start voice chat" waveform (several separate bars) is clicked with `oma-vclick`, but only after
two shots in a row agree on its position; the send arrow means a draft, and nothing is clicked.
It counts as started only when a NEW Grok Bot microphone stream appears within 6 seconds of the
click (`pactl`, compared with the streams open before). Otherwise it retries once, then hides the
overlay, focuses Grok Bot and notifies "Click the voice button to talk".

While the overlay is up, the listener keeps watching that same new microphone stream. Once it has
been gone for about 1.5 seconds — you said goodbye, or you hung up — the overlay hides and
Grok Bot is focused again. Esc, any key, a click or a mouse move still dismiss it immediately.
`oma-voice-wake test-end-monitor` dry-runs this logic with a fake clock and a scripted mic
(no wake, no overlay, no click).

## Commands

```
oma-voice-wake status | wifi | test-wake [secs] | trigger <name>
oma-voice-wake test-end-monitor
oma-voice-wake passcode set | clear | status
oma-voice-wake unlock-listen | forget-button
journalctl --user -u oma-voice-wake -f
```

## Settings reference (`~/.config/oma/voice-wake.json`)

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | listen at all |
| `agents[]` | | `name`, `phrase`, `agentId`, `enabled`, optional `alternatives[]` |
| `sensitivity` | `0.5` | 0 strict … 1 easy (confidence threshold and energy gate) |
| `overlay` | `true` | black wake screen |
| `startVoice` | `true` | press the voice button after opening the agent |
| `openUrl` | `grokbot://app/v1/agent?id={id}` | how the agent is opened |
| `settleMs` | `1400` | wait after the window appears before looking for the button |
| `voiceUnlock.enabled` | `false` | allow unlocking by voice |
| `voiceUnlock.trustedWifi` | `[]` | connection names or SSIDs |
| `voiceUnlock.maxTries` / `lockoutMinutes` | `3` / `5` | misses before typed-only, and for how long |
| `voiceUnlock.requireTypedSinceBoot` | `true` | voice unlock off after boot until a typed unlock |
| `voiceUnlock.lockScreenButton` | `true` | mic button on the lock screen |
| `prompts` | `true` | spoken prompts |

## Privacy

Audio goes from PipeWire into the local recognizer in 100 ms chunks and is dropped. Only speech-like
stretches are decoded; only the matched agent and a confidence go to the journal. Passcode
transcripts are compared and discarded, never logged.

## Uninstall

```sh
systemctl --user disable --now oma-voice-wake.service
omarchy plugin disable johnatfreecoffee.voice-wake
rm -rf ~/.local/share/oma-voice-wake ~/.local/state/oma-voice-wake ~/.local/bin/oma-voice-wake \
       ~/.config/omarchy/plugins/johnatfreecoffee.voice-wake ~/.config/systemd/user/oma-voice-wake.service
```

## Credits

[Vosk](https://alphacephei.com/vosk/) (Apache-2.0), [Piper](https://github.com/rhasspy/piper),
wlr-protocols. Code: Apache-2.0. See NOTICE.
