# Security notes

**A spoken passcode is weak security.** Anyone who hears it, or plays back a recording of it, can
unlock the computer while voice unlock is allowed. Any program already running as your user could
also write the unlock token and call the lock's `voiceUnlock` IPC. Use voice unlock only where you
accept that.

What the add-on does to limit it:

- Voice unlock is **off by default** and does nothing until you set a passcode.
- The passcode is set **by typing only** and stored as a **salted scrypt hash**
  (`~/.local/state/oma-voice-wake/passcode.json`, mode 600). The plain text is never written,
  logged, or passed on a command line (the CLI reads it from stdin).
- Matching is exact (lower-cased words) on an **offline Vosk transcript**. No AI or LLM, nothing
  leaves the machine, and the transcript is discarded right after the comparison.
- Only on **trusted Wi-Fi** networks you list (`voiceUnlock.trustedWifi`). Network names can be
  spoofed; treat this as a convenience fence, not a security boundary.
- **Off after every boot** until you have typed your password once (the lock writes a marker in
  `$XDG_RUNTIME_DIR`, which is cleared on reboot).
- **Three misses, then 5 minutes typed-only.**
- The unlock token is random, single use, 0600, and expires after 10 seconds.
- Every voice unlock shows a notification. Typing your password always works.

Report problems through GitHub issues on this repository.
