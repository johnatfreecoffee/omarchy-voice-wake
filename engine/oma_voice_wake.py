#!/usr/bin/env python3
"""oma-voice-wake: offline "Hey <agent>" voice wake for Omarchy + the Grok Bot desktop app.

  oma-voice-wake run                 the listener (run by systemd --user: oma-voice-wake.service)
  oma-voice-wake status              JSON: enabled, agents, passcode set, Wi-Fi trusted, lockout
  oma-voice-wake passcode set        read the passcode twice from stdin (never argv); store a salted scrypt hash
  oma-voice-wake passcode clear      remove the stored hash
  oma-voice-wake passcode check-heard  (Settings) say if the recognizer can hear the passcode, via local TTS, in memory
  oma-voice-wake unlock-listen       ask the listener to check a spoken passcode now (lock-screen mic button)
  oma-voice-wake test-wake [secs]    listen once and report what wake phrase was heard (no launch)
  oma-voice-wake trigger <name>      act as if "Hey <name>" was heard (testing, hotkeys)
  oma-voice-wake test-end-monitor    dry-run: simulate mic appear/disappear and check overlay auto-hide
  oma-voice-wake test-find-button [ppm]  dry-run the voice-button locator (synthetic; optional composer strip)
  oma-voice-wake wifi                print the active Wi-Fi network name(s)
  oma-voice-wake forget-button       drop the learned Grok Bot voice-button position

Speech is recognized fully on this machine with Vosk (small English model) using a restricted
grammar for the wake phrases. The voice passcode is matched exactly (normalized words) against a
salted scrypt hash: no AI, no LLM, nothing leaves the machine. The plain passcode is never written,
logged or put on a command line."""
import hashlib, hmac, json, os, queue, secrets, shlex, shutil, socket, subprocess, sys, threading, time, wave, re, getpass

HOME = os.path.expanduser("~")
APP = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("OMA_VOICE_WAKE_CONFIG", os.path.join(HOME, ".config/oma/voice-wake.json"))
STATE = os.environ.get("OMA_VOICE_WAKE_STATE", os.path.join(HOME, ".local/state/oma-voice-wake"))
RUN = os.environ.get("OMA_VOICE_WAKE_RUN", os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "oma-voice-wake"))
PASSFILE = os.path.join(STATE, "passcode.json")
CALIB = os.path.join(STATE, "voice-button.json")
SOCK = os.path.join(RUN, "sock")
STATUS = os.path.join(RUN, "status.json")
LEVEL = os.path.join(RUN, "level")
TOKEN = os.path.join(RUN, "unlock-token")
TYPED = os.path.join(RUN, "typed-unlock")        # written by the lock screen after a typed unlock
LOCKOUT = os.path.join(RUN, "lockout.json")
MODEL_DIR = os.environ.get("OMA_VOICE_WAKE_MODEL", os.path.join(APP, "models/vosk-model-small-en-us-0.15"))
PROMPTS = os.path.join(APP, "prompts")
VOICE = os.path.join(APP, "voices/en_US-amy-low.onnx")
VCLICK = os.path.join(APP, "bin/oma-vclick")
RATE = 16000
CHUNK = 1600  # 100 ms of 16-bit mono
# After a hand-off, hide the black overlay when Grok Bot's voice chat ends (mic stream gone)
# or if a stream never appears. Mouse/key dismissal in the overlay still works.
OVERLAY_MIC_GONE_S = 1.5
OVERLAY_MIC_APPEAR_S = 20.0
OVERLAY_POLL_S = 0.2
# Cold-launch and composer readiness (Grok Bot may not already be open).
WINDOW_WAIT_S = 20.0
COMPOSER_WAIT_S = 20.0
# The whole "Starting voice…" stage is capped (the overlay plugin enforces the same 25 s).
OVERLAY_START_MAX_S = 25.0
# After clicking "Start voice chat", a NEW Grok Bot mic stream must appear within this time.
VOICE_CONFIRM_S = 6.0
DEBUG_SHOT = os.path.join(STATE, "last-handoff.png")
MATCH_SCORE = 0.85
MATCH_GLYPH = 0.75
TEMPLATE_SCALES = (0.80, 0.90, 1.00, 1.10, 1.20)

DEFAULTS = {
    "enabled": True,
    "agents": [],            # [{"name": "Jack", "phrase": "hey jack", "agentId": "...", "enabled": true}]
    "sensitivity": 0.5,      # 0..1, higher wakes more easily
    "overlay": True,
    "openUrl": "grokbot://app/v1/agent?id={id}",
    "startVoice": True,      # click Grok Bot's voice button after opening the agent
    "settleMs": 1400,
    "voiceUnlock": {"enabled": False, "trustedWifi": [], "maxTries": 3, "lockoutMinutes": 5,
                    "requireTypedSinceBoot": True, "lockScreenButton": True},
    "prompts": True,
}
DISTRACTORS = ["hey", "okay", "hey you", "hey man", "hey there", "hey guys", "hey jake", "hey jackie",
               "hey jacky", "black jack", "hey john", "hey mom", "hey ma", "hey siri", "hey google",
               "alexa", "hi", "hello", "thank you", "yeah", "no", "hey hey"]
_DIGITS = "zero one two three four five six seven eight nine".split()


def log(*a):
    print("oma-voice-wake:", *a, flush=True)


# ------------------------------------------------------------------ config + files
def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        log("config unreadable, using defaults:", e)
    return cfg


def write_private(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def normalize(text):
    """Lower-case words only; digits become number words (that is how the recognizer writes them)."""
    t = (text or "").lower().replace("'", "")
    t = re.sub(r"[0-9]", lambda m: " " + _DIGITS[int(m.group())] + " ", t)
    t = re.sub(r"[^a-z ]+", " ", t)
    return " ".join(t.split())


# ------------------------------------------------------------------ passcode (salted scrypt)
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def _derive(secret, salt, p=SCRYPT):
    return hashlib.scrypt(secret.encode(), salt=salt, n=p["n"], r=p["r"], p=p["p"], dklen=p["dklen"], maxmem=64 * 1024 * 1024)


def passcode_set(secret):
    norm = normalize(secret)
    if not norm:
        raise ValueError("The passcode needs at least one word.")
    salt = secrets.token_bytes(16)
    rec = {"v": 1, "algo": "scrypt", **SCRYPT, "salt": salt.hex(), "hash": _derive(norm, salt).hex(), "words": len(norm.split()), "set": int(time.time())}
    write_private(PASSFILE, json.dumps(rec) + "\n")


def passcode_record():
    try:
        with open(PASSFILE) as f:
            return json.load(f)
    except Exception:
        return None


def passcode_matches(heard):
    rec = passcode_record()
    norm = normalize(heard)
    if not rec or not norm:
        return False
    p = {k: rec[k] for k in ("n", "r", "p", "dklen")}
    return hmac.compare_digest(_derive(norm, bytes.fromhex(rec["salt"]), p), bytes.fromhex(rec["hash"]))


# ------------------------------------------------------------------ environment probes
def sh(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def session_locked():
    out = sh(["omarchy-shell", "lock", "isLocked"], 3)
    return out.strip('"') == "true"


def active_wifi():
    names = set()
    for line in sh(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"]).splitlines():
        name, _, typ = line.rpartition(":")
        if typ == "802-11-wireless":
            name = name.replace("\\:", ":")
            names.add(name)
            ssid = sh(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", name])
            if ssid:
                names.add(ssid)
    return sorted(names)


def wifi_trusted(cfg):
    trusted = set(cfg["voiceUnlock"].get("trustedWifi") or [])
    return bool(trusted & set(active_wifi()))


def lockout_until():
    try:
        with open(LOCKOUT) as f:
            return float(json.load(f).get("until", 0))
    except Exception:
        return 0.0


def unlock_blockers(cfg):
    """Why voice unlock can't be used right now (empty list = allowed)."""
    vu = cfg["voiceUnlock"]
    why = []
    if not cfg.get("enabled") or not vu.get("enabled"):
        why.append("off")
    if not passcode_record():
        why.append("no-passcode")
    if lockout_until() > time.time():
        why.append("lockout")
    if vu.get("requireTypedSinceBoot", True) and not os.path.exists(TYPED):
        why.append("typed-first")
    if not wifi_trusted(cfg):
        why.append("untrusted-wifi")
    return why


def notify(title, body="", urgency="normal"):
    subprocess.Popen(["notify-send", "-a", "Voice Wake", "-u", urgency, "-i", "audio-input-microphone", title, body],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def shell_ipc(*args):
    return sh(["omarchy-shell", "-q", "voicewake", *args], 3)


# ------------------------------------------------------------------ audio
class Mic(threading.Thread):
    """16 kHz mono from PipeWire's default source; 100 ms chunks onto a queue."""

    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue(maxsize=200)
        self.level = 0.0
        self.proc = None

    def run(self):
        try:
            import warnings
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            import audioop
            rms_of = lambda c: float(audioop.rms(c, 2))
        except Exception:  # Python 3.13+: no audioop
            import array, math
            def rms_of(c):
                a = array.array("h", c)
                return math.sqrt(sum(x * x for x in a) / max(1, len(a)))
        while True:
            self.proc = subprocess.Popen(["pw-record", "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16",
                                          "--media-role", "Communication", "-P", "{ node.name = oma-voice-wake media.name = \"Voice Wake\" }", "-"],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            buf = b""
            while True:
                data = self.proc.stdout.read(CHUNK * 2)
                if not data:
                    break
                buf += data
                while len(buf) >= CHUNK * 2:
                    c, buf = buf[:CHUNK * 2], buf[CHUNK * 2:]
                    rms = rms_of(c)
                    self.level = rms
                    try:
                        self.q.put_nowait((c, rms))
                    except queue.Full:
                        try:
                            self.q.get_nowait()
                        except queue.Empty:
                            pass
            log("microphone stream ended; restarting in 2 s")
            time.sleep(2)

    def flush(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break


def play(name, cfg=None):
    if cfg is not None and not cfg.get("prompts", True):
        return
    p = os.path.join(PROMPTS, name + ".wav")
    if os.path.exists(p):
        subprocess.run(["pw-play", "--media-role", "Notification", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


class Gate:
    """Energy gate: only speech-like stretches reach the recognizer, so silence costs ~no CPU."""

    def __init__(self, sensitivity):
        self.floor = 150.0
        self.factor = 3.6 - 1.6 * max(0.0, min(1.0, sensitivity))
        self.minimum = 220.0

    def threshold(self):
        return max(self.floor * self.factor, self.minimum)

    def quiet(self, rms):
        self.floor = 0.97 * self.floor + 0.03 * min(rms, self.floor * 3)


def utterances(mic, gate, max_s=4.0, end_silence_s=0.7, start_timeout=None, stop=None):
    """Yield lists of chunks, one per speech stretch (with 400 ms pre-roll)."""
    pre, cur, silent, t0 = [], None, 0, time.time()
    while True:
        if cur is None and stop is not None and stop():
            return
        try:
            c, rms = mic.q.get(timeout=0.5)
        except queue.Empty:
            if start_timeout and cur is None and time.time() - t0 > start_timeout:
                return
            continue
        if cur is None:
            if rms > gate.threshold():
                cur, silent = pre + [c], 0
                pre = []
            else:
                gate.quiet(rms)
                pre = (pre + [c])[-4:]
                if start_timeout and time.time() - t0 > start_timeout:
                    return
            continue
        cur.append(c)
        silent = silent + 1 if rms < gate.threshold() * 0.8 else 0
        if silent * 0.1 >= end_silence_s or len(cur) * 0.1 >= max_s:
            yield cur
            cur, silent, t0 = None, 0, time.time()


# ------------------------------------------------------------------ recognizer
class Ears:
    def __init__(self):
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        self.model = Model(MODEL_DIR)
        self.cfg_sig = None
        self.wake = None
        self.phrases = {}

    def configure(self, cfg):
        from vosk import KaldiRecognizer
        agents = [a for a in cfg.get("agents", []) if a.get("enabled", True) and normalize(a.get("phrase", ""))]
        sig = json.dumps([(normalize(a["phrase"]), a.get("name")) for a in agents])
        if sig == self.cfg_sig:
            return
        self.cfg_sig = sig
        self.phrases = {}
        for a in agents:
            self.phrases[normalize(a["phrase"])] = a
            for alt in a.get("alternatives", []) or []:
                if normalize(alt):
                    self.phrases[normalize(alt)] = a
        grammar = sorted(set(self.phrases) | (set(DISTRACTORS) - set(self.phrases))) + ["[unk]"]
        self.wake = KaldiRecognizer(self.model, RATE, json.dumps(grammar))
        self.wake.SetWords(True)
        log("listening for:", ", ".join(sorted(self.phrases)) or "(no agents)")

    def match_wake(self, chunks, threshold):
        """Return (agent, confidence, heard) or (None, conf, heard)."""
        r = self.wake
        r.Reset()
        for c in chunks:
            r.AcceptWaveform(c)
        res = json.loads(r.FinalResult())
        words = res.get("result", [])
        heard = " ".join(w["word"] for w in words)
        toks = [w for w in words]
        for phrase, agent in self.phrases.items():
            pw = phrase.split()
            if len(toks) >= len(pw) and [t["word"] for t in toks[:len(pw)]] == pw:
                conf = min(t.get("conf", 0) for t in toks[:len(pw)])
                return (agent if conf >= threshold else None), conf, heard
        return None, 0.0, heard

    def transcribe(self, chunks):
        from vosk import KaldiRecognizer
        r = KaldiRecognizer(self.model, RATE)
        for c in chunks:
            r.AcceptWaveform(c)
        return json.loads(r.FinalResult()).get("text", "")


# ------------------------------------------------------------------ Grok Bot hand-off
def hypr_json(what):
    try:
        return json.loads(sh(["hyprctl", what, "-j"]) or "null")
    except Exception:
        return None


def hypr_lua(*exprs):
    """Run Hyprland dispatchers. This Hyprland takes Lua (hl.dsp.*) through `hyprctl repl`."""
    if not exprs:
        return
    sh(["hyprctl", "repl", " ".join(f"pcall(hl.dispatch, {e})" for e in exprs)], 3)


def _waddr(addr):
    return f'"address:{addr}"'


def _is_grok(c):
    return "grok" in (c.get("class") or "").lower()


def grok_window():
    """The main Grok Bot window: the focused one if Grok Bot is focused, else the largest."""
    aw = hypr_json("activewindow") or {}
    if _is_grok(aw) and aw.get("size"):
        return aw
    wins = [c for c in hypr_json("clients") or [] if _is_grok(c) and c.get("size")]
    if not wins:
        return None
    return max(wins, key=lambda c: c["size"][0] * c["size"][1])


def hypr_find(addr):
    return next((c for c in hypr_json("clients") or [] if c.get("address") == addr), None)


def focused_monitor():
    ms = hypr_json("monitors") or []
    return next((m for m in ms if m.get("focused")), ms[0] if ms else None)


def monitor_box(m):
    """Logical (x, y, w, h) of a monitor: what hyprctl window coordinates and grim -g use."""
    sc = float(m.get("scale") or 1.0)
    w, h = m["width"], m["height"]
    if int(m.get("transform") or 0) % 2 == 1:
        w, h = h, w
    return m["x"], m["y"], w / sc, h / sc


def _on_screen(win, m):
    mx, my, mw, mh = monitor_box(m)
    (x, y), (w, h) = win["at"], win["size"]
    ws = win.get("workspace") or {}
    return (not str(ws.get("name", "")).startswith("special:")
            and ws.get("id") == (m.get("activeWorkspace") or {}).get("id")
            and x >= mx - 4 and y >= my - 4 and x + w <= mx + mw + 4 and y + h <= my + mh + 4)


def bring_grok_forward(win, timeout_s=4.0):
    """Put the Grok Bot window on the visible workspace, fully on screen, focused and settled.

    Oma's Stage Manager parks apps that aren't on stage in special:oma-stage (and animates them
    in from off screen), so a just-opened Grok Bot can be invisible to a screenshot. Ask Stage
    Manager to promote it the same way a click in its strip does; fall back to moving it.
    """
    addr = win.get("address")
    if not addr:
        return win
    m = focused_monitor()
    if not m:
        return win
    ws = (m.get("activeWorkspace") or {}).get("name") or str((m.get("activeWorkspace") or {}).get("id", 1))
    stage = os.path.join(HOME, ".local/bin/oma-stage")
    if os.access(stage, os.X_OK):
        sh([stage, "promote", win.get("class", "")], 3)
    t0, last, nudged = time.time(), None, False
    while time.time() - t0 < timeout_s:
        time.sleep(0.25)
        w = hypr_find(addr)
        if not w:
            return None
        m = focused_monitor() or m
        if str((w.get("workspace") or {}).get("name", "")).startswith("special:") and time.time() - t0 > 0.8:
            hypr_lua(f'hl.dsp.window.move({{ workspace = "{ws}", follow = false, window = {_waddr(addr)} }})')
            continue
        geo = (tuple(w["at"]), tuple(w["size"]), (w.get("workspace") or {}).get("id"))
        if _on_screen(w, m):
            if geo == last:
                if (hypr_json("activewindow") or {}).get("address") != addr:
                    hypr_lua(f"hl.dsp.focus({{ window = {_waddr(addr)} }})")
                return w
        elif time.time() - t0 > 1.5 and not nudged and w.get("floating"):
            # Stuck off screen (e.g. an interrupted Stage Manager animation): center it.
            nudged = True
            mx, my, mw, mh = monitor_box(m)
            ww, wh = min(w["size"][0], int(mw * 0.9)), min(w["size"][1], int(mh * 0.9))
            hypr_lua(f"hl.dsp.window.resize({{ x = {ww}, y = {wh}, relative = false, window = {_waddr(addr)} }})",
                     f"hl.dsp.window.move({{ x = {int(mx + (mw - ww) / 2)}, y = {int(my + (mh - wh) / 2)}, relative = false, window = {_waddr(addr)} }})")
        last = geo
        hypr_lua(f"hl.dsp.focus({{ window = {_waddr(addr)} }})")
    return hypr_find(addr) or win


def _grok_props(p):
    blob = " ".join(str(p.get(k, "")) for k in ("application.process.binary", "application.name"))
    return "grok" in blob.lower()


def grok_streams():
    """Indexes of the Grok Bot app's open microphone streams (PipeWire source-outputs)."""
    try:
        outs = json.loads(sh(["pactl", "-f", "json", "list", "source-outputs"]) or "[]")
    except Exception:
        return set()
    return {o.get("index") for o in outs if _grok_props(o.get("properties", {}))}


def grok_recording():
    """True when the Grok Bot app has any microphone stream open."""
    return bool(grok_streams())


def overlay_visible():
    """True/False from the shell plugin; None if the IPC didn't answer."""
    # Do not use omarchy-shell -q here: quiet mode suppresses the method's return value.
    out = sh(["omarchy-shell", "voicewake", "isVisible"], 3)
    if out in ("true", "1"):
        return True
    if out in ("false", "0"):
        return False
    return None


def focus_grok():
    """Put keyboard focus back on the Grok Bot window after the overlay goes away."""
    win = grok_window() or {}
    if win.get("address"):
        hypr_lua(f"hl.dsp.focus({{ window = {_waddr(win['address'])} }})")


def hide_overlay():
    shell_ipc("hide")
    focus_grok()


def watch_for_voice_end(*, already_live, appear_s=OVERLAY_MIC_APPEAR_S, gone_s=OVERLAY_MIC_GONE_S,
                        poll_s=OVERLAY_POLL_S, recording_fn=None, visible_fn=None,
                        sleep_fn=None, clock_fn=None, stop_fn=None):
    """Decide when the wake overlay should hide, without driving the real screen.

    Returns one of:
      mic-ended        Grok Bot had a mic stream, then it was gone for gone_s
      never-appeared   no mic stream within appear_s of the start
      already-hidden   the overlay was dismissed first (mouse, key, or hide)
      stopped          stop_fn became true (a newer wake cancelled this watch)

    recording_fn / visible_fn / sleep_fn / clock_fn are injectable so the logic can be
    exercised with a fake clock and a scripted mic, with no real wake or overlay.
    """
    recording_fn = recording_fn or grok_recording
    visible_fn = visible_fn or overlay_visible
    sleep_fn = sleep_fn or time.sleep
    clock_fn = clock_fn or time.time

    def still_shown():
        v = visible_fn()
        return True if v is None else v

    def cancelled():
        return bool(stop_fn and stop_fn())

    t0 = clock_fn()
    seen = bool(already_live) or bool(recording_fn())
    if not seen:
        while True:
            if cancelled():
                return "stopped"
            if not still_shown():
                return "already-hidden"
            now = clock_fn()
            if now - t0 >= appear_s:
                return "never-appeared"
            if recording_fn():
                seen = True
                break
            sleep_fn(poll_s)

    gone_since = None
    while True:
        if cancelled():
            return "stopped"
        if not still_shown():
            return "already-hidden"
        now = clock_fn()
        if recording_fn():
            gone_since = None
        else:
            if gone_since is None:
                gone_since = now
            elif now - gone_since >= gone_s:
                return "mic-ended"
        sleep_fn(poll_s)


class _FakeClock:
    def __init__(self, start=0.0):
        self.t = float(start)

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)


def test_end_monitor():
    """Scripted dry-run of watch_for_voice_end. No mic, no overlay, no Grok Bot click."""
    cases = []

    def case(name, expect, already, intervals, visible_until=10**9, appear=20.0, gone=1.5, stop_at=None, poll=0.2):
        clock = _FakeClock()
        def rec():
            t = clock.time()
            return any(a <= t < b for a, b in intervals)
        def vis():
            return clock.time() < visible_until
        stop = (lambda: clock.time() >= stop_at) if stop_at is not None else None
        got = watch_for_voice_end(already_live=already, appear_s=appear, gone_s=gone, poll_s=poll,
                                  recording_fn=rec, visible_fn=vis, sleep_fn=clock.sleep,
                                  clock_fn=clock.time, stop_fn=stop)
        ok = got == expect
        cases.append((ok, name, expect, got, clock.time()))
        print(("ok" if ok else "FAIL") + f": {name} -> {got} (expected {expect}) at t={clock.time():.2f}s")

    # Live from the start, ends at 5s, hide after 1.5s of silence.
    case("mic ended after a live call", "mic-ended", True, [(0, 5)])
    # A short dropout must not hide the overlay.
    case("brief mic dropout is ignored", "mic-ended", True, [(0, 2), (2.4, 5)])
    # Safety timeout: no stream ever appears.
    case("no mic within 20s", "never-appeared", False, [])
    # Stream shows up late (user clicked the button), then the call ends.
    case("mic appears later then ends", "mic-ended", False, [(3, 8)])
    # User dismissed with the mouse/key while the call was still live.
    case("mouse/key already hid it", "already-hidden", True, [(0, 50)], visible_until=2.0)
    # already_live is false, but the first poll already sees the stream.
    case("first poll already live", "mic-ended", False, [(0, 4)])
    # A newer wake cancels this watch.
    case("cancelled by a newer wake", "stopped", True, [(0, 50)], stop_at=1.0)
    # Thought it was live, but the stream is already gone: still wait the grace period.
    case("already-live but stream already gone", "mic-ended", True, [])

    failed = [c for c in cases if not c[0]]
    print(f"{len(cases) - len(failed)}/{len(cases)} passed")
    return 0 if not failed else 1


def grab(x, y, w, h):
    import numpy as np
    raw = subprocess.run(["grim", "-g", f"{x},{y} {w}x{h}", "-t", "ppm", "-"], capture_output=True, timeout=5).stdout
    return load_ppm(raw)


def load_ppm(raw):
    """Decode a binary P6 PPM (from grim or a file) into a float32 HxWx3 array."""
    import numpy as np
    if not raw.startswith(b"P6"):
        raise ValueError("not a binary PPM")
    parts = raw.split(b"\n", 3)
    # Skip comment lines if present between header fields.
    rest = raw[2:]
    if rest.startswith(b"\n"):
        rest = rest[1:]
    while rest.startswith(b"#"):
        rest = rest.split(b"\n", 1)[1]
    header, rest = rest.split(b"\n", 1)
    w, h = map(int, header.split())
    if rest.startswith(b"255\n"):
        body = rest[4:]
    else:
        # maxval line may follow
        mv, body = rest.split(b"\n", 1)
        if mv.strip() != b"255":
            raise ValueError("only 8-bit PPMs are supported")
    return np.frombuffer(body[:w * h * 3], dtype=np.uint8).reshape(h, w, 3).astype(np.float32)


def _resize(arr, scale):
    """Bilinear resize of an HxW or HxWxC float array. Scale 1 is a no-op."""
    import numpy as np
    if abs(scale - 1.0) < 1e-6:
        return arr
    h, w = arr.shape[:2]
    nh = max(8, int(round(h * scale)))
    nw = max(8, int(round(w * scale)))
    ys = (np.arange(nh) + 0.5) * (h / nh) - 0.5
    xs = (np.arange(nw) + 0.5) * (w / nw) - 0.5
    y0 = np.clip(np.floor(ys).astype(int), 0, h - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, w - 1)
    y1 = np.clip(y0 + 1, 0, h - 1)
    x1 = np.clip(x0 + 1, 0, w - 1)
    wy = (ys - y0).reshape(nh, 1)
    wx = (xs - x0).reshape(1, nw)
    if arr.ndim == 2:
        Ia = arr[y0][:, x0]; Ib = arr[y0][:, x1]; Ic = arr[y1][:, x0]; Id = arr[y1][:, x1]
        return ((Ia * (1 - wx) + Ib * wx) * (1 - wy) + (Ic * (1 - wx) + Id * wx) * wy).astype(np.float32)
    wy = wy[:, :, None]; wx = wx[:, :, None]
    Ia = arr[y0][:, x0]; Ib = arr[y0][:, x1]; Ic = arr[y1][:, x0]; Id = arr[y1][:, x1]
    return ((Ia * (1 - wx) + Ib * wx) * (1 - wy) + (Ic * (1 - wx) + Id * wx) * wy).astype(np.float32)


def _ncc(a, b):
    a = a - a.mean(); b = b - b.mean()
    d = (float((a * a).sum()) * float((b * b).sum())) ** 0.5
    return float((a * b).sum() / d) if d else 0.0


def match_template(img, tpl, cx=None, cy=None, radius=70, step=2):
    """Best normalized cross-correlation of `tpl` in `img`.

    If (cx, cy) is given, search a neighbourhood of `radius` pixels; otherwise search the
    whole image. Returns (score, inner, (x, y)): `inner` scores only the glyph in the middle,
    so a send arrow on the same dark disc doesn't pass.
    """
    import numpy as np
    th, tw = tpl.shape[:2]
    H, W = img.shape[:2]
    t = tpl.mean(axis=2) if tpl.ndim == 3 else tpl
    g = img.mean(axis=2) if img.ndim == 3 else img
    if cx is None or cy is None:
        y_lo, y_hi = 0, max(0, H - th)
        x_lo, x_hi = 0, max(0, W - tw)
        step = max(step, 3)
    else:
        y_lo = max(0, int(cy - radius - th / 2))
        y_hi = max(y_lo, min(H - th, int(cy + radius - th / 2)))
        x_lo = max(0, int(cx - radius - tw / 2))
        x_hi = max(x_lo, min(W - tw, int(cx + radius - tw / 2)))
    best, bxy = -1.0, None
    for y in range(y_lo, y_hi + 1):
        for x in range(x_lo, x_hi + 1, step):
            sc = _ncc(g[y:y + th, x:x + tw], t)
            if sc > best:
                best, bxy = sc, (x, y)
    if bxy is None:
        return -1.0, -1.0, None
    x, y = bxy
    # refine on neighbouring columns (and a 1-px row nudge) around the best stepped hit
    for yy in (y - 1, y, y + 1):
        if not (0 <= yy <= H - th):
            continue
        for xx in range(x - step, x + step + 1):
            if 0 <= xx <= W - tw:
                sc = _ncc(g[yy:yy + th, xx:xx + tw], t)
                if sc > best:
                    best, x, y = sc, xx, yy
    q = max(1, th // 4)
    inner = _ncc(g[y + q:y + th - q, x + q:x + tw - q], t[q:th - q, q:tw - q])
    return best, inner, (x + tw / 2.0, y + th / 2.0)


def score_template(img, tpl, cx=None, cy=None, radius=70, scales=TEMPLATE_SCALES):
    """Best (score, inner, xy, scale) of `tpl` over a few sizes, so a scale change still hits."""
    best = (-1.0, -1.0, None, 1.0)
    for sc in scales:
        t = _resize(tpl, sc)
        score, inner, xy = match_template(img, t, cx, cy, radius=radius)
        if score > best[0] or (score == best[0] and inner > best[1]):
            best = (score, inner, xy, sc)
    return best


def round_controls(img, band=130, contrast=8):
    """Circular controls in the bottom `band` pixels. Low contrast so a pale mic on white still counts.

    Returns a list of dicts, right-most first: cx, cy, bw, bh, fill, mean (luma).
    """
    import numpy as np
    H, W, _ = img.shape
    y_off = max(0, H - band)
    g = img[y_off:].mean(axis=2)
    bg = float(np.median(g))
    mask = np.abs(g - bg) > contrast
    h, w = mask.shape
    seen = np.zeros_like(mask)
    found = []
    ys, xs = np.nonzero(mask)
    for i in range(len(xs)):
        y0, x0 = int(ys[i]), int(xs[i])
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]; seen[y0, x0] = True
        minx = maxx = x0; miny = maxy = y0; n = 0
        while stack:
            y, x = stack.pop(); n += 1
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
            if n > 8000:
                break
            for yy, xx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True; stack.append((yy, xx))
        bw, bh = maxx - minx + 1, maxy - miny + 1
        if bw == 0 or bh == 0:
            continue
        fill = n / float(bw * bh)
        if 16 <= bw <= 64 and 16 <= bh <= 64 and 0.70 <= bw / bh <= 1.40 and 0.50 <= fill <= 0.95:
            region = img[miny + y_off:maxy + 1 + y_off, minx:maxx + 1]
            found.append({"cx": (minx + maxx) / 2.0, "cy": (miny + maxy) / 2.0 + y_off,
                          "bw": bw, "bh": bh, "fill": fill, "mean": float(region.mean())})
    found.sort(key=lambda c: -c["cx"])
    return found


def glyph_parts(img, c):
    """Count the separate marks drawn inside a round control (its glyph).

    Grok Bot's composer has one call-to-action disc on the right: the "Start voice chat"
    waveform (several separate bars) when the composer is empty, or the send arrow (one mark)
    with the pale dictation mic beside it when there is a draft. Bars vs one mark tells them apart.
    """
    import numpy as np
    r = 0.36 * min(c["bw"], c["bh"])
    y0, y1 = int(c["cy"] - r), int(c["cy"] + r) + 1
    x0, x1 = int(c["cx"] - r), int(c["cx"] + r) + 1
    H, W = img.shape[:2]
    if y0 < 0 or x0 < 0 or y1 > H or x1 > W:
        return 0
    g = img[y0:y1, x0:x1].mean(axis=2)
    fill = float(np.median(g))
    # Anti-aliased gaps between bars sit between the disc and the glyph colour: cut halfway.
    far = float(g.max()) if abs(float(g.max()) - fill) >= abs(float(g.min()) - fill) else float(g.min())
    if abs(far - fill) < 60:
        return 0
    mask = np.abs(g - fill) > 0.55 * abs(far - fill)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    mask &= (xx - c["cx"]) ** 2 + (yy - c["cy"]) ** 2 <= r * r
    seen = np.zeros_like(mask)
    parts = 0
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        parts_n, stack = 0, [(y, x)]
        seen[y, x] = True
        while stack:
            a, b = stack.pop(); parts_n += 1
            for aa, bb in ((a + 1, b), (a - 1, b), (a, b + 1), (a, b - 1)):
                if 0 <= aa < mask.shape[0] and 0 <= bb < mask.shape[1] and mask[aa, bb] and not seen[aa, bb]:
                    seen[aa, bb] = True; stack.append((aa, bb))
        if parts_n >= 3:
            parts += 1
    return parts


def find_voice_button(img, band=130):
    """Classify the composer's right-hand call-to-action.

    Returns (kind, (cx, cy) or None, detail):
      voice    the "Start voice chat" waveform button (safe to click)
      draft    the send arrow: a draft is in the composer, so Grok Bot shows no voice-chat button
               (the pale mic beside it is dictation, not voice chat, and is never clicked)
      none     no composer controls found yet
    """
    # The call-to-action sits at the right end of the composer: look only at the right half, so
    # the sidebar doesn't skew the background estimate.
    x_off = img.shape[1] // 2 if img.shape[1] >= 400 else 0
    ctrls = round_controls(img[:, x_off:], band=band)
    for c in ctrls:
        c["cx"] += x_off
    if not ctrls:
        return "none", None, "no controls"
    cta = ctrls[0]
    parts = glyph_parts(img, cta)
    beside = [c for c in ctrls[1:] if abs(c["cy"] - cta["cy"]) <= 12
              and 0.6 * cta["bw"] <= (cta["cx"] - c["cx"]) <= 3.0 * cta["bw"]]
    detail = f"cta at {cta['cx']:.0f},{cta['cy']:.0f} {cta['bw']}x{cta['bh']} marks={parts} beside={len(beside)}"
    if parts >= 3:
        return "voice", (cta["cx"], cta["cy"]), detail
    if beside:
        return "draft", None, detail
    return "none", None, detail


def save_png(path, img):
    """Write an HxWx3 array as PNG (no PIL needed). For the hand-off diagnosis screenshot."""
    import numpy as np, zlib, struct
    a = np.clip(img, 0, 255).astype(np.uint8)
    h, w = a.shape[:2]
    raw = b"".join(b"\x00" + a[y].tobytes() for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def shoot_window(win, tag=""):
    """grim the Grok Bot window's real geometry (clipped to its monitor). The overlay must already
    be hidden (setCapture). Returns (img, rx, ry, k): k = image pixels per logical pixel."""
    m = focused_monitor()
    mx, my, mw, mh = monitor_box(m) if m else (0, 0, 10 ** 5, 10 ** 5)
    (wx, wy), (ww, wh) = win["at"], win["size"]
    x0, y0 = max(wx, mx), max(wy, my)
    x1, y1 = min(wx + ww, mx + mw), min(wy + wh, my + mh)
    if x1 - x0 < 200 or y1 - y0 < 150:
        raise RuntimeError(f"Grok Bot window is off screen (at {wx},{wy} size {ww}x{wh})")
    x0, y0, rw, rh = int(x0), int(y0), int(x1 - x0), int(y1 - y0)
    raw = subprocess.run(["grim", "-g", f"{x0},{y0} {rw}x{rh}", "-t", "ppm", "-"], capture_output=True, timeout=5).stdout
    img = load_ppm(raw)
    k = img.shape[1] / float(rw)
    try:
        save_png(DEBUG_SHOT, img)
        if tag:
            save_png(DEBUG_SHOT.replace(".png", f"-{tag}.png"), img)
    except Exception as e:
        log("debug screenshot not saved:", e)
    return img, x0, y0, k


def launch_grok(url):
    """Open the agent. xdg-open first; the grok-bot binary is a fallback if the window is late."""
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return shutil.which("grok-bot") or ("/usr/bin/grok-bot" if os.path.isfile("/usr/bin/grok-bot") else None)


def wait_for_grok_window(timeout_s=WINDOW_WAIT_S, url=None, bin_path=None, wanted=None):
    """Poll Hyprland clients until a Grok Bot window exists (any workspace, incl. Stage Manager's)."""
    t0 = time.time()
    launched_bin = False
    while time.time() - t0 < timeout_s:
        if wanted and not wanted():
            return None
        time.sleep(0.3)
        win = grok_window()
        if win and win.get("size") and win["size"][0] > 200:
            return win
        if (not launched_bin) and bin_path and url and (time.time() - t0 >= 3.0):
            launched_bin = True
            try:
                subprocess.Popen([bin_path, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log("grok-bot launch failed:", e)
    return None


def start_voice(cfg, agent, wanted=None, deadline=None):
    """Open the agent's chat (cold-launching Grok Bot if needed) and press "Start voice chat".

    Returns (outcome, new_mic_streams). outcome: voice-live | draft | button-not-found |
    clicked-unconfirmed | no-window | dismissed | opened | no-agent-id.
    voice-live means a NEW Grok Bot microphone stream appeared after our click.
    """
    wanted = wanted or (lambda: True)
    deadline = deadline or (time.time() + OVERLAY_START_MAX_S - 2)
    aid = agent.get("agentId", "")
    if not aid or "TODO" in aid:
        notify(f"Hey {agent.get('name')}", "This agent's Grok Bot id isn't set yet (Settings > Voice Wake).")
        return "no-agent-id", set()
    url = cfg.get("openUrl", DEFAULTS["openUrl"]).replace("{id}", aid)
    was_open = grok_window() is not None
    bin_path = launch_grok(url)
    if not cfg.get("startVoice", True):
        return "opened", set()
    win = wait_for_grok_window(min(WINDOW_WAIT_S, max(1, deadline - time.time())), url=url, bin_path=bin_path, wanted=wanted)
    if not wanted():
        return "dismissed", set()
    if not win:
        return "no-window", set()
    if not was_open:
        time.sleep(1.5)  # cold launch: let it route to the agent's chat before we look
    settle = float(cfg.get("settleMs", 1400)) / 1000.0

    # Grok Bot keeps showing the previous chat for a moment after the link: never judge before this.
    if was_open:
        time.sleep(1.8)
    for attempt in (1, 2):
        before = grok_streams()
        target, seen_at, kind, detail = None, None, "none", ""
        draft_since = None
        while True:
            if not wanted():
                return "dismissed", set()
            if time.time() >= deadline:
                break
            win = bring_grok_forward(grok_window() or win) or win
            shell_ipc("setCapture", "true")
            time.sleep(0.2)
            try:
                img, rx, ry, k = shoot_window(win, tag=f"try{attempt}")
                kind, xy, detail = find_voice_button(img, band=int(160 * k))
                if kind == "voice":
                    pos = (rx + xy[0] / k, ry + xy[1] / k)
                    # Require the same spot on two shots in a row so a half-rendered or
                    # still-routing window is never clicked.
                    if seen_at and abs(seen_at[0] - pos[0]) <= 4 and abs(seen_at[1] - pos[1]) <= 4:
                        target = pos
                        subprocess.run([VCLICK, str(int(round(pos[0]))), str(int(round(pos[1])))], timeout=5)
                    seen_at = pos
                else:
                    seen_at = None
            except Exception as e:
                log("screenshot failed:", e)
                kind = "none"
            finally:
                shell_ipc("setCapture", "false")
            if target:
                break
            if kind == "draft":
                # Only believe "draft" once it has held for 2 s: the previous chat may still be on screen.
                draft_since = draft_since or time.time()
                if time.time() - draft_since >= 2.0:
                    break
            else:
                draft_since = None
            time.sleep(0.35)
        log(f"voice button (try {attempt}):", kind, detail, "-> clicked" if target else "")
        if kind == "draft":
            return "draft", set()
        if target is None:
            return "button-not-found", set()
        # Confirm: a NEW Grok Bot mic stream (not one that was already open before the click).
        new = set()
        t0 = time.time()
        while time.time() - t0 < VOICE_CONFIRM_S:
            time.sleep(0.25)
            new = grok_streams() - before
            if new:
                break
        if new:
            log("voice live: new Grok Bot mic stream", sorted(new))
            return "voice-live", new
        log(f"voice button (try {attempt}): no new mic stream within {VOICE_CONFIRM_S:.0f}s")
        if time.time() + VOICE_CONFIRM_S + 2 > deadline:
            break
        time.sleep(settle / 2)
    return "clicked-unconfirmed", set()


def _draw_disc(img, cx, cy, r, color):
    import numpy as np
    H, W = img.shape[:2]
    y0, y1 = max(0, int(cy - r - 1)), min(H, int(cy + r + 2))
    x0, x1 = max(0, int(cx - r - 1)), min(W, int(cx + r + 2))
    yy, xx = np.ogrid[y0:y1, x0:x1]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    img[y0:y1, x0:x1][mask] = color


def _synthetic_composer(kind):
    """Tiny white strip used by test-find-button. No real Grok Bot window is touched."""
    import numpy as np
    H, W = 130, 400
    img = np.full((H, W, 3), 252.0, dtype=np.float32)
    cy, send_cx, mic_cx, r = 100.0, 340.0, 307.0, 12.5
    if kind in ("draft", "both"):
        _draw_disc(img, send_cx, cy, r, (28, 28, 28))
        # white upward arrow (send glyph) so a waveform template would fail the inner score
        for dy in range(-5, 6):
            w = 1 if dy < 0 else 3
            x0, x1 = int(send_cx - w), int(send_cx + w + 1)
            y = int(cy + dy)
            img[y, x0:x1] = 245
        _draw_disc(img, mic_cx, cy, r, (230, 230, 230))
        # darker mic capsule
        _draw_disc(img, mic_cx, cy - 1, 3.2, (120, 120, 120))
        img[int(cy + 2):int(cy + 6), int(mic_cx - 1):int(mic_cx + 2)] = 120
        return img, (mic_cx, cy)
    # empty composer: one dark waveform disc on the right
    _draw_disc(img, send_cx, cy, r, (28, 28, 28))
    for dx, h in ((-6, 2), (-3, 4), (0, 6), (3, 4), (6, 2)):  # separate waveform bars
        img[int(cy - h):int(cy + h) + 1, int(send_cx + dx):int(send_cx + dx) + 2] = 220
    return img, (send_cx, cy)


def test_find_button(path=None):
    """Dry-run the locator on synthetic composer strips (and optionally a PNG/PPM screenshot)."""
    cases = []

    def check(name, img, expect_kind, expect_xy=None, tol=8):
        kind, xy, detail = find_voice_button(img)
        ok = kind == expect_kind and (expect_xy is None or (xy and abs(xy[0] - expect_xy[0]) <= tol
                                                             and abs(xy[1] - expect_xy[1]) <= tol))
        cases.append(ok)
        print(("ok" if ok else "FAIL") + f": {name} -> {kind} {xy} ({detail})")

    img, expect = _synthetic_composer("empty")
    check("empty composer: click the waveform (Start voice chat)", img, "voice", expect)
    img, _ = _synthetic_composer("draft")
    check("draft: send arrow + dictation mic, never clicked", img, "draft")
    import numpy as np
    check("blank window: nothing to click", np.full((130, 400, 3), 252.0, dtype=np.float32), "none")
    if path:
        raw = open(path, "rb").read()
        if raw.startswith(b"P6"):
            shot = load_ppm(raw)
        else:
            shot = load_png(raw)
        kind, xy, detail = find_voice_button(shot, band=min(shot.shape[0], 200))
        print(f"file {path}: {shot.shape[1]}x{shot.shape[0]} -> {kind} {xy} ({detail})")
    print(f"{sum(cases)}/{len(cases)} passed")
    return 0 if all(cases) else 1


def load_png(raw):
    """Minimal 8-bit RGB/RGBA non-interlaced PNG reader (for test-find-button on saved shots)."""
    import numpy as np, zlib, struct
    pos, idat, w = 8, b"", 0
    while pos < len(raw):
        n, t = struct.unpack(">I4s", raw[pos:pos + 8]); d = raw[pos + 8:pos + 8 + n]; pos += 12 + n
        if t == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", d[:10])
        elif t == b"IDAT":
            idat += d
    ch = {2: 3, 6: 4}[ctype]
    data = zlib.decompress(idat); stride = w * ch
    out = np.zeros((h, stride), dtype=np.int32); prev = np.zeros(stride, dtype=np.int32)
    for y in range(h):
        f = data[y * (stride + 1)]; row = np.frombuffer(data[y * (stride + 1) + 1:(y + 1) * (stride + 1)], dtype=np.uint8).astype(np.int32)
        if f == 0: cur = row
        elif f == 2: cur = (row + prev) & 255
        else:
            cur = np.zeros(stride, dtype=np.int32)
            for x in range(stride):
                a = cur[x - ch] if x >= ch else 0; b = prev[x]; c = prev[x - ch] if x >= ch else 0
                if f == 1: p = a
                elif f == 3: p = (a + b) // 2
                else:
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    p = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                cur[x] = (row[x] + p) & 255
        out[y] = cur; prev = cur
    return out.reshape(h, w, ch)[:, :, :3].astype(np.float32)


class Listener:
    def __init__(self):
        os.makedirs(RUN, mode=0o700, exist_ok=True)
        os.chmod(RUN, 0o700)
        self.cfg = load_config()
        self.cfg_mtime = 0
        self.mic = Mic()
        self.ears = Ears()
        self.ears.configure(self.cfg)
        self.gate = Gate(self.cfg.get("sensitivity", 0.5))
        self.cmds = queue.Queue()
        self.phase = "idle"
        self.status_cache = None
        self.level_until = 0
        self.test_until = 0
        self._overlay_gen = 0

    # status for the lock screen + Settings
    def write_status(self, force=False):
        blockers = unlock_blockers(self.cfg)
        st = {"phase": self.phase,
              "lockButton": bool(self.cfg["voiceUnlock"].get("lockScreenButton", True)) and not blockers,
              "blockers": blockers, "passcodeSet": passcode_record() is not None,
              "agents": [a.get("name") for a in self.cfg.get("agents", []) if a.get("enabled", True)]}
        s = json.dumps(st)
        if force or s != self.status_cache:
            self.status_cache = s
            write_private(STATUS, s + "\n", 0o600)

    def set_phase(self, p):
        self.phase = p
        self.write_status()

    def reload_if_changed(self):
        try:
            m = os.stat(CONFIG).st_mtime
        except FileNotFoundError:
            m = 0
        if m != self.cfg_mtime:
            self.cfg_mtime = m
            self.cfg = load_config()
            self.ears.configure(self.cfg)
            self.gate.factor = Gate(self.cfg.get("sensitivity", 0.5)).factor

    def serve(self):
        try:
            os.unlink(SOCK)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(SOCK)
        os.chmod(SOCK, 0o600)
        s.listen(4)
        while True:
            c, _ = s.accept()
            try:
                line = c.recv(256).decode(errors="ignore").strip()
                self.cmds.put(line)
                c.sendall(b"ok\n")
            finally:
                c.close()

    def levels(self):
        while True:
            if time.time() < self.level_until:
                try:
                    with open(LEVEL, "w") as f:
                        f.write(f"{min(1.0, self.mic.level / 3000.0):.3f}\n")
                except Exception:
                    pass
                time.sleep(0.08)
            else:
                time.sleep(0.5)

    def threshold(self):
        return 0.95 - 0.4 * max(0.0, min(1.0, float(self.cfg.get("sensitivity", 0.5))))

    def run(self):
        self.mic.start()
        threading.Thread(target=self.serve, daemon=True).start()
        threading.Thread(target=self.levels, daemon=True).start()
        self.write_status(force=True)
        last_tick = 0
        stop_now = lambda: not self.cmds.empty() or time.time() - last_tick > 10
        while True:
            last_tick = time.time()
            self.reload_if_changed()
            self.write_status()
            while not self.cmds.empty():
                self.command(self.cmds.get())
            if not self.cfg.get("enabled", True) or not self.ears.phrases:
                self.mic.flush()
                time.sleep(1)
                continue
            for utt in utterances(self.mic, self.gate, max_s=3.0, end_silence_s=0.5, stop=stop_now):
                agent, conf, heard = self.ears.match_wake(utt, self.threshold())
                if time.time() < self.test_until:
                    self.test_until = 0
                    notify("Voice Wake test", f"Heard: “{heard or '…'}” (confidence {conf:.2f})" + (f" → {agent['name']}" if agent else " → no wake"))
                    continue
                if agent:
                    log(f"wake: {agent.get('name')} (confidence {conf:.2f})")
                    self.wake(agent)
                    self.mic.flush()
                break

    def command(self, line):
        parts = line.split(None, 1)
        if not parts:
            return
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        if cmd == "unlock-listen":
            if session_locked():
                self.unlock_flow(None)
        elif cmd == "test-wake":
            self.test_until = time.time() + float(arg or 8)
            notify("Voice Wake test", "Say your wake phrase now…")
        elif cmd == "trigger":
            for a in self.cfg.get("agents", []):
                if a.get("name", "").lower() == arg.lower():
                    self.wake(a)
        elif cmd == "reload":
            self.cfg_mtime = -1
            self.reload_if_changed()

    # wake → (unlock) → overlay → Grok Bot voice
    def wake(self, agent):
        if session_locked():
            # light the (possibly blanked) lock screen so the prompt and mic state are visible
            subprocess.Popen(["omarchy-system-wake"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not self.unlock_flow(agent):
                return
        self.handoff(agent)

    def unlock_flow(self, agent):
        blockers = unlock_blockers(self.cfg)
        if blockers:
            msg = {"off": None, "no-passcode": None, "lockout": "locked_out", "typed-first": "typed_first",
                   "untrusted-wifi": "untrusted"}
            for b in blockers:
                if msg.get(b):
                    play(msg[b], self.cfg)
                    break
            if agent is not None:
                self.wait_for_typed_unlock(agent)
            return False
        tries = int(self.cfg["voiceUnlock"].get("maxTries", 3))
        for attempt in range(tries):
            self.set_phase("ask")
            play("ask" if attempt == 0 else "retry", self.cfg)
            self.mic.flush()
            self.set_phase("listening")
            said = None
            for utt in utterances(self.mic, self.gate, max_s=4.0, end_silence_s=0.8, start_timeout=5):
                said = utt
                break
            if said is None:
                continue
            self.set_phase("checking")
            heard = self.ears.transcribe(said)
            ok = passcode_matches(heard)
            heard = None  # never kept, logged or shown
            if ok:
                if self.unlock():
                    self.set_phase("ok")
                    notify("Unlocked with your voice passcode", time.strftime("%-I:%M %p"))
                    log("voice unlock: ok")
                    self.set_phase("idle")
                    return True
                log("voice unlock: passcode ok but the lock refused")
                break
            log("voice unlock: miss")
            self.set_phase("miss")
        until = time.time() + 60 * float(self.cfg["voiceUnlock"].get("lockoutMinutes", 5))
        write_private(LOCKOUT, json.dumps({"until": until}))
        play("locked_out", self.cfg)
        notify("Voice unlock paused", f"Too many tries. Type your password; voice works again after {time.strftime('%-I:%M %p', time.localtime(until))}.")
        self.set_phase("idle")
        return False

    def unlock(self):
        tok = secrets.token_urlsafe(24)
        write_private(TOKEN, f"{tok} {int((time.time() + 10) * 1000)}\n")
        out = sh(["omarchy-shell", "lock", "voiceUnlock", tok], 5).strip('"')
        try:
            os.unlink(TOKEN)
        except FileNotFoundError:
            pass
        return out == "ok"

    def wait_for_typed_unlock(self, agent):
        """Off trusted Wi-Fi (or before the first typed unlock) the wake is remembered for 60 s: type your
        password and the agent still opens."""
        def waiter():
            t0 = time.time()
            while time.time() - t0 < 60:
                time.sleep(1)
                if not session_locked():
                    self.handoff(agent)
                    return
        threading.Thread(target=waiter, daemon=True).start()

    def handoff(self, agent):
        name = agent.get("name", "")
        aid = agent.get("agentId", "")
        if not aid or "TODO" in aid:
            notify(f"Hey {name}", "This agent's Grok Bot id isn't set yet (Settings > Voice Wake > Agents).")
            log("hand-off:", name, "no agent id")
            return
        overlay = self.cfg.get("overlay", True)
        self._overlay_gen += 1  # cancels any older overlay watcher
        t_start = time.time()
        if overlay:
            shell_ipc("show", json.dumps({"name": name}))
            self.level_until = time.time() + 600
        # The overlay's own Esc / mouse / key dismissal (or its 25 s cap) aborts the hand-off.
        wanted = (lambda: overlay_visible() is not False) if overlay else (lambda: True)
        try:
            outcome, streams = start_voice(self.cfg, agent, wanted=wanted, deadline=t_start + OVERLAY_START_MAX_S - 2)
        except Exception as e:
            outcome, streams = f"error: {e}", set()
        log("hand-off:", name, outcome, f"({time.time() - t_start:.1f}s)")
        if outcome == "voice-live":
            if overlay:
                shell_ipc("setLive", "true")
                self.watch_overlay_end(streams)
            return
        self.level_until = 0
        if overlay:
            hide_overlay()
        else:
            focus_grok()
        if outcome == "draft":
            notify(f"Hey {name}", "There's an unsent message in this chat, so Grok Bot hides its voice button. "
                                  "Send or clear it, then click the voice button (or say the wake phrase again).")
        elif outcome in ("button-not-found", "clicked-unconfirmed") or outcome.startswith("error"):
            notify(f"Hey {name}", "Click the voice button to talk")
        elif outcome == "no-window":
            notify(f"Hey {name}", "Grok Bot didn't open. Open it and click the voice button to talk.")

    def watch_overlay_end(self, streams):
        """While the overlay is up, hide it once the voice chat's own mic stream(s) are gone."""
        gen = self._overlay_gen
        streams = set(streams or ())

        def recording():
            return bool(grok_streams() & streams) if streams else grok_recording()

        def watcher():
            reason = watch_for_voice_end(already_live=True, recording_fn=recording,
                                         stop_fn=lambda: gen != self._overlay_gen)
            if gen != self._overlay_gen:
                return
            self.level_until = 0
            if reason in ("mic-ended", "never-appeared"):
                log("overlay hide:", reason)
                hide_overlay()
            elif reason == "already-hidden":
                log("overlay hide: already-hidden")
                focus_grok()

        threading.Thread(target=watcher, daemon=True, name="oma-overlay-watch").start()


# ------------------------------------------------------------------ CLI
def send(cmd):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(SOCK)
    s.sendall((cmd + "\n").encode())
    s.recv(16)
    s.close()


def status():
    cfg = load_config()
    out = {"enabled": cfg.get("enabled"), "agents": [{k: a.get(k) for k in ("name", "phrase", "enabled")} | {"agentIdSet": bool(a.get("agentId")) and "TODO" not in a.get("agentId", "")} for a in cfg.get("agents", [])],
           "passcodeSet": passcode_record() is not None, "wifi": active_wifi(), "wifiTrusted": wifi_trusted(cfg),
           "voiceUnlockBlockers": unlock_blockers(cfg), "listenerRunning": os.path.exists(SOCK),
           "voiceButtonLearned": os.path.exists(CALIB)}
    print(json.dumps(out, indent=2))


def check_heard(secret):
    """Speak the passcode with the local TTS voice into memory and see if the recognizer hears the same
    words. Nothing is written to disk."""
    import numpy as np
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    p = subprocess.run([os.path.join(APP, "venv/bin/piper"), "-m", VOICE, "--output-raw"], input=secret.encode(),
                       capture_output=True, timeout=60)
    pcm = p.stdout
    r = KaldiRecognizer(Model(MODEL_DIR), 16000)
    for i in range(0, len(pcm), 8000):
        r.AcceptWaveform(pcm[i:i + 8000])
    ok = normalize(json.loads(r.FinalResult()).get("text", "")) == normalize(secret)
    pcm = None
    return ok


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip()); return 0
    cmd = argv[1]
    if cmd == "run":
        Listener().run()
    elif cmd == "status":
        status()
    elif cmd == "wifi":
        print("\n".join(active_wifi()))
    elif cmd == "passcode":
        sub = argv[2] if len(argv) > 2 else ""
        if sub == "set":
            if sys.stdin.isatty():
                a = getpass.getpass("New voice passcode: "); b = getpass.getpass("Again: ")
            else:
                lines = sys.stdin.read().split("\n")
                a, b = (lines + ["", ""])[:2]
            if a != b:
                print("The two entries don't match.", file=sys.stderr); return 1
            try:
                passcode_set(a)
            except ValueError as e:
                print(e, file=sys.stderr); return 1
            a = b = None
            print("saved")
        elif sub == "clear":
            try:
                os.unlink(PASSFILE)
            except FileNotFoundError:
                pass
            print("cleared")
        elif sub == "status":
            print("set" if passcode_record() else "not set")
        elif sub == "check-heard":
            secret = sys.stdin.readline().rstrip("\n")
            print("heard" if check_heard(secret) else "not-heard")
            secret = None
        else:
            print("usage: passcode set|clear|status|check-heard", file=sys.stderr); return 2
        try:
            send("reload")
        except Exception:
            pass
    elif cmd in ("unlock-listen", "reload"):
        send(cmd)
    elif cmd == "test-wake":
        send("test-wake " + (argv[2] if len(argv) > 2 else "8"))
    elif cmd == "trigger":
        send("trigger " + " ".join(argv[2:]))
    elif cmd == "forget-button":
        try:
            os.unlink(CALIB)
        except FileNotFoundError:
            pass
        print("forgotten")
    elif cmd == "test-end-monitor":
        return test_end_monitor()
    elif cmd == "test-find-button":
        return test_find_button(argv[2] if len(argv) > 2 else None)
    else:
        print(__doc__.strip()); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
