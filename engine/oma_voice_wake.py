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


def grok_window():
    aw = hypr_json("activewindow") or {}
    if "grok" in (aw.get("class") or "").lower():
        return aw
    for c in hypr_json("clients") or []:
        if "grok" in (c.get("class") or "").lower():
            return c
    return None


def grok_recording():
    """True when the Grok Bot app has a microphone stream open (its voice chat is live)."""
    try:
        outs = json.loads(sh(["pactl", "-f", "json", "list", "source-outputs"]) or "[]")
    except Exception:
        return False
    for o in outs:
        p = o.get("properties", {})
        blob = " ".join(str(p.get(k, "")) for k in ("application.process.binary", "application.name", "application.process.id"))
        if "grok" in blob.lower() or str(p.get("application.process.binary", "")).lower() in ("grok-bot",):
            return True
    return False


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
    addr = win.get("address")
    if not addr:
        return
    try:
        subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{addr}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass


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


def find_voice_button(img, band=130):
    """Locate the voice-chat control in the composer row.

    Empty composer: one dark round control on the right (waveform). Draft present: a pale
    mic button sits just left of the dark send arrow — return the mic, never the arrow.
    Returns (cx, cy) in img coords or None.
    """
    ctrls = round_controls(img, band=band)
    if not ctrls:
        return None
    right = ctrls[0]
    # A neighbour just to the left of the right-most disc is the mic beside a draft's send button.
    lefts = [c for c in ctrls[1:]
             if abs(c["cy"] - right["cy"]) <= 12
             and 0.6 * right["bw"] <= (right["cx"] - c["cx"]) <= 3.0 * right["bw"]]
    if lefts:
        mic = max(lefts, key=lambda c: c["cx"])
        return mic["cx"], mic["cy"]
    return right["cx"], right["cy"]


def locate_voice_button(img, cal, ww, strip_h):
    """Pick a click target. Returns (xy, how, kind) or (None, reason, None).

    kind is 'empty' (dark waveform button) or 'draft' (mic beside send), used when relearning.
    """
    import numpy as np
    hits = []

    def consider(tpl, px, py, radius, label, kind):
        score, inner, xy, scale = score_template(img, tpl, px, py, radius=radius)
        hits.append((label, score, inner, xy, scale, kind))
        return score, inner, xy, scale

    if cal and cal.get("template") is not None:
        tpl = np.array(cal["template"], dtype=np.float32)
        px, py = ww - cal["fromRight"], strip_h - cal["fromBottom"]
        score, inner, xy, scale = consider(tpl, px, py, 70, "template", "empty")
        if xy and score >= MATCH_SCORE and inner >= MATCH_GLYPH:
            return xy, f"template {score:.2f}/{inner:.2f}@{scale:.2f}", "empty"
        # High outer / low glyph at the usual spot is the send arrow (a draft is in the composer).
        # Only do a slow whole-strip search when the disc itself wasn't where we expected.
        if not (xy and score >= MATCH_SCORE):
            score, inner, xy, scale = consider(tpl, None, None, 70, "template-wide", "empty")
            if xy and score >= MATCH_SCORE and inner >= MATCH_GLYPH:
                return xy, f"template-wide {score:.2f}/{inner:.2f}@{scale:.2f}", "empty"
        log("voice button match",
            f"{hits[0][1]:.2f} / glyph {hits[0][2]:.2f}"
            + (f", wide {score:.2f}/{inner:.2f}" if len(hits) > 1 else ""))

    if cal and cal.get("templateDraft") is not None:
        tpl = np.array(cal["templateDraft"], dtype=np.float32)
        px, py = ww - cal["fromRightDraft"], strip_h - cal["fromBottomDraft"]
        score, inner, xy, scale = consider(tpl, px, py, 90, "draft", "draft")
        if not (xy and score >= MATCH_SCORE and inner >= MATCH_GLYPH):
            score, inner, xy, scale = consider(tpl, None, None, 90, "draft-wide", "draft")
        if xy and score >= MATCH_SCORE and inner >= MATCH_GLYPH:
            return xy, f"draft-template {score:.2f}/{inner:.2f}@{scale:.2f}", "draft"

    xy = find_voice_button(img)
    if xy:
        kind = "draft" if len(round_controls(img)) >= 2 else "empty"
        return xy, "heuristic", kind
    return None, "button-not-found", None


def learn_button(img, cx, cy, ww, strip_h, kind="empty"):
    """Save a 36x36 template. 'draft' is stored alongside the empty-composer button, not over it."""
    x0, y0 = int(round(cx - 18)), int(round(cy - 18))
    H, W = img.shape[:2]
    if y0 < 0 or x0 < 0 or y0 + 36 > H or x0 + 36 > W:
        return
    tpl = img[y0:y0 + 36, x0:x0 + 36].round().astype(int).tolist()
    rec = {}
    try:
        with open(CALIB) as f:
            rec = json.load(f)
    except Exception:
        pass
    now = int(time.time())
    if kind == "draft":
        rec.update({"fromRightDraft": ww - cx, "fromBottomDraft": strip_h - cy,
                    "templateDraft": tpl, "learnedDraft": now})
    else:
        rec.update({"fromRight": ww - cx, "fromBottom": strip_h - cy,
                    "template": tpl, "learned": now})
    write_private(CALIB, json.dumps(rec))


def launch_grok(url):
    """Open the agent. xdg-open first; the grok-bot binary is a fallback if the window is late."""
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return shutil.which("grok-bot") or ("/usr/bin/grok-bot" if os.path.isfile("/usr/bin/grok-bot") else None)


def wait_for_grok_window(timeout_s=WINDOW_WAIT_S, url=None, bin_path=None):
    """Poll Hyprland clients (not just the active window) until Grok Bot is mapped, up to timeout_s.

    If the window still isn't up after 3 seconds and bin_path is set, start grok-bot with the URL
    (a second launch of the already-running app just focuses it).
    """
    t0 = time.time()
    launched_bin = False
    while time.time() - t0 < timeout_s:
        time.sleep(0.3)
        win = grok_window() or {}
        if win.get("mapped") and not win.get("hidden"):
            return win
        # Also accept a window that has a size even if 'mapped' is missing from older hyprctl.
        if win.get("size") and not win.get("hidden"):
            return win
        if (not launched_bin) and bin_path and url and (time.time() - t0 >= 3.0):
            launched_bin = True
            try:
                subprocess.Popen([bin_path, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log("grok-bot launch failed:", e)
    return None


def grab_composer(win):
    (wx, wy), (ww, wh) = win["at"], win["size"]
    strip_h = min(260, wh)
    sy = wy + wh - strip_h
    shell_ipc("setCapture", "true")
    time.sleep(0.18)
    try:
        img = grab(wx, sy, ww, strip_h)
    finally:
        shell_ipc("setCapture", "false")
    return img, wx, wy, ww, wh, strip_h, sy


def start_voice(cfg, agent):
    """Open the agent's chat (cold-launching Grok Bot if needed) and press its voice button."""
    aid = agent.get("agentId", "")
    if not aid or "TODO" in aid:
        notify(f"Hey {agent.get('name')}", "This agent's Grok Bot id isn't set yet (Settings > Voice Wake).")
        return "no-agent-id"
    url = cfg.get("openUrl", DEFAULTS["openUrl"]).replace("{id}", aid)
    bin_path = launch_grok(url)
    if not cfg.get("startVoice", True):
        return "opened"
    win = wait_for_grok_window(WINDOW_WAIT_S, url=url, bin_path=bin_path)
    if not win:
        return "no-window"
    if grok_recording():
        return "already-live"

    cal = None
    try:
        with open(CALIB) as f:
            cal = json.load(f)
    except Exception:
        pass

    target, how, kind = None, "", None
    img = wx = sy = ww = strip_h = None
    t1 = time.time()
    while True:
        win = grok_window() or win
        try:
            img, wx, wy, ww, wh, strip_h, sy = grab_composer(win)
        except Exception as e:
            log("composer grab failed:", e)
            img = None
        if img is not None:
            if grok_recording():
                return "already-live"
            target, how, kind = locate_voice_button(img, cal, ww, strip_h)
            if target:
                break
        if time.time() - t1 >= COMPOSER_WAIT_S:
            break
        time.sleep(0.4)

    if target is None:
        return "button-not-found"
    cx, cy = target
    subprocess.run([VCLICK, str(int(wx + cx)), str(int(sy + cy))], timeout=5)
    ok = False
    for _ in range(16):
        time.sleep(0.25)
        if grok_recording():
            ok = True
            break
    if ok:
        # Relearn only after a confirmed mic stream, and never overwrite the other state's template.
        if kind == "draft" and (how == "heuristic" or how.startswith("draft")):
            learn_button(img, cx, cy, ww, strip_h, kind="draft")
        elif kind == "empty" and (how == "heuristic" or not cal or cal.get("template") is None):
            learn_button(img, cx, cy, ww, strip_h, kind="empty")
        elif kind == "empty" and how == "heuristic" and cal:
            # Template missed (scale / layout) but the heuristic found the empty-state button.
            learn_button(img, cx, cy, ww, strip_h, kind="empty")
    log("voice button:", how, "->", "voice live" if ok else "no mic stream seen")
    return "voice-live" if ok else "clicked-unconfirmed"


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
    for dx in range(-5, 6):
        h = 3 + int(3 * abs((dx % 3) - 1))
        img[int(cy - h):int(cy + h) + 1, int(send_cx + dx)] = 220
    return img, (send_cx, cy)


def test_find_button(path=None):
    """Dry-run the locator. With no args, uses synthetic composer strips. Optional PPM path."""
    import numpy as np
    cases = []

    def check(name, img, expect, tol=8):
        xy = find_voice_button(img)
        ok = xy is not None and abs(xy[0] - expect[0]) <= tol and abs(xy[1] - expect[1]) <= tol
        cases.append((ok, name, expect, xy))
        print(("ok" if ok else "FAIL") + f": {name} -> {xy} (expected near {expect})")

    img, expect = _synthetic_composer("draft")
    check("synthetic draft: mic left of send, not the arrow", img, expect)
    # The right-most disc must not be returned as the click target when a mic sits beside it.
    ctrls = round_controls(img)
    if len(ctrls) >= 2 and expect[0] < ctrls[0]["cx"]:
        cases.append((True, "draft has two controls and send is to the right", None, None))
        print("ok: draft has two controls and send is to the right")
    else:
        cases.append((False, "draft has two controls and send is to the right", "2 ctrls, send right of mic", ctrls))
        print("FAIL: draft controls", [(round(c["cx"], 1), round(c["cy"], 1), c["bw"], c["bh"]) for c in ctrls])

    img, expect = _synthetic_composer("empty")
    check("synthetic empty: the lone dark waveform disc", img, expect)

    # A learned waveform template must reject the send arrow (high outer / low glyph).
    empty, _ = _synthetic_composer("empty")
    draft, _ = _synthetic_composer("draft")
    ex, ey = 340.0, 100.0
    tpl = empty[int(ey - 18):int(ey + 18), int(ex - 18):int(ex + 18)]
    score, inner, xy = match_template(draft, tpl, ex, ey, radius=40)
    ok = inner < MATCH_GLYPH
    cases.append((ok, "send arrow fails glyph check", f"inner<{MATCH_GLYPH}", f"{score:.2f}/{inner:.2f} at {xy}"))
    print(("ok" if ok else "FAIL") + f": send arrow fails glyph check -> {score:.2f}/{inner:.2f} at {xy}")

    if path:
        raw = open(path, "rb").read()
        if raw.startswith(b"P6"):
            shot = load_ppm(raw)
        else:
            raise SystemExit(f"test-find-button: {path} is not a binary PPM (convert with: ffmpeg -i src.png -c:v ppm dst.ppm)")
        xy = find_voice_button(shot)
        print(f"file {path}: {shot.shape[1]}x{shot.shape[0]} -> {xy}")
        # If the strip looks like a draft (two right-side controls), the click target must be
        # the left of the right-most pair, not the send arrow.
        ctrls = [c for c in round_controls(shot) if c["cx"] > shot.shape[1] * 0.5]
        if len(ctrls) >= 2 and xy is not None:
            send = max(ctrls, key=lambda c: c["cx"])
            ok = xy[0] < send["cx"] - 8
            cases.append((ok, f"file draft: clicked left of send ({send['cx']:.0f})", True, xy))
            print(("ok" if ok else "FAIL") + f": file draft -> {xy} (send at {send['cx']:.0f},{send['cy']:.0f})")
        elif xy is None:
            cases.append((False, "file: no control found", "a button", None))
            print("FAIL: file: no control found")
        else:
            print(f"(file has {len(ctrls)} right-side control(s); not treated as a draft pair)")

    failed = [c for c in cases if not c[0]]
    print(f"{len(cases) - len(failed)}/{len(cases)} passed")
    return 0 if not failed else 1


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
        if self.cfg.get("overlay", True):
            shell_ipc("show", json.dumps({"name": name}))
            self.level_until = time.time() + 600
        try:
            outcome = start_voice(self.cfg, agent)
        except Exception as e:
            outcome = f"error: {e}"
        log("hand-off:", name, outcome)
        hide_now = outcome in ("button-not-found", "no-window") or outcome.startswith("error")
        if hide_now and self.cfg.get("overlay", True):
            # Don't leave John on the black "move the mouse" screen when the click never happened.
            hide_overlay()
        elif self.cfg.get("overlay", True):
            shell_ipc("setLive", "true")
            already_live = outcome in ("voice-live", "already-live")
            self.watch_overlay_end(already_live)
        if outcome == "button-not-found":
            notify(f"Hey {name}", "Click the voice button to talk")
        elif outcome == "no-window":
            notify(f"Hey {name}", "Grok Bot didn't open. Open it and click the voice button to talk.")
        elif outcome == "clicked-unconfirmed" or outcome.startswith("error"):
            notify(f"Hey {name}", "Opened the chat. Click the voice button to talk (I couldn't start voice by myself).")

    def watch_overlay_end(self, already_live):
        """While the overlay is up, hide it when Grok Bot's voice chat ends (or never starts)."""
        self._overlay_gen += 1
        gen = self._overlay_gen

        def watcher():
            reason = watch_for_voice_end(already_live=already_live,
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
