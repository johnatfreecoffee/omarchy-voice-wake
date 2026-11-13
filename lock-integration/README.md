# Lock-screen integration (voice unlock + mic button)

Omarchy 4.5's lock screen is a Quickshell plugin (`omarchy.lock`). Voice unlock needs three small
additions, made in **your own clone** of that plugin so Omarchy updates never overwrite them:

1. `voiceUnlock(token)` on the `lock` IPC target. The listener writes a random one-time token to
   `$XDG_RUNTIME_DIR/oma-voice-wake/unlock-token` (0600, 10 s life) and calls
   `omarchy-shell lock voiceUnlock <token>`; the lock compares, deletes the file, and unlocks.
2. After a **typed** password unlock it writes `$XDG_RUNTIME_DIR/oma-voice-wake/typed-unlock`, so
   voice unlock stays off after every boot until you type once.
3. A round **mic button** under the password field, shown only when the listener's
   `status.json` says voice unlock is allowed right now. Clicking it runs
   `oma-voice-wake unlock-listen`.

## Apply

```sh
omarchy plugin clone omarchy.lock                   # makes ~/.config/omarchy/plugins/$USER.lock and switches to it
cd ~/.config/omarchy/plugins/$USER.lock
cp Service.qml Service.qml.bak-$(date +%s); cp LockView.qml LockView.qml.bak-$(date +%s)
patch -p1 < /path/to/omarchy-voice-wake/lock-integration/Service.patch
patch -p1 < /path/to/omarchy-voice-wake/lock-integration/LockView.patch
omarchy-restart-shell                               # refuses while locked; run it unlocked
```

`Service.patch` is against Omarchy 4.5.0-3's stock `plugins/lock/Service.qml`; `LockView.patch`
against the stock `LockView.qml`. If your lock is already customized, apply the hunks by hand.
Without this step the wake word, overlay and Grok Bot hand-off still work; only unlocking by
voice doesn't (you type your password and the agent opens right after).
