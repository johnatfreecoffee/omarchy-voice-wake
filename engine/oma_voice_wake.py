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
  oma-voice-wake wifi                print the active Wi-Fi network name(s)
  oma-voice-wake forget-button       drop the learned Grok Bot voice-button position

Speech is recognized fully on this machine with Vosk (small English model) using a restricted
grammar for the wake phrases. The voice passcode is matched exactly (normalized words) against a
salted scrypt hash: no AI, no LLM, nothing leaves the machine. The plain passcode is never written,
logged or put on a command line."""
import hashlib, hmac, json, os, queue, secrets, shlex, socket, subprocess, sys, threading, time, wave, re, getpass

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


def grab(x, y, w, h):
    import numpy as np
    raw = subprocess.run(["grim", "-g", f"{x},{y} {w}x{h}", "-t", "ppm", "-"], capture_output=True, timeout=5).stdout
    # P6\n<w> <h>\n255\n
    parts = raw.split(b"\n", 3)
    W, H = map(int, parts[1].split())
    return np.frombuffer(parts[3][:W * H * 3], dtype=np.uint8).reshape(H, W, 3).astype(np.float32)


def find_voice_button(img, band=130):
    """Heuristic for an un-calibrated first run: in the composer row at the very bottom of the window,
    the voice-chat button is the right-most filled round control (a dark disc with a waveform).
    Returns (cx, cy) in img coords or None."""
    import numpy as np
    H, W, _ = img.shape
    y_off = max(0, H - band)
    g = img[y_off:].mean(axis=2)
    bg = float(np.median(g))
    mask = np.abs(g - bg) > 60
    h, w = mask.shape
    seen = np.zeros_like(mask)
    best = None
    ys, xs = np.nonzero(mask)
    order = np.argsort(-xs)               # right-most pixels first
    for idx in order:
        y0, x0 = int(ys[idx]), int(xs[idx])
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
            if n > 6000:
                break
            for yy, xx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True; stack.append((yy, xx))
        bw, bh = maxx - minx + 1, maxy - miny + 1
        fill = n / float(bw * bh)
        if 18 <= bw <= 64 and 18 <= bh <= 64 and 0.75 <= bw / bh <= 1.33 and 0.55 <= fill <= 0.95:
            cand = ((minx + maxx) / 2.0, (miny + maxy) / 2.0 + y_off)
            if best is None or cand[0] > best[0]:
                best = cand
            break                          # the right-most qualifying blob wins
    return best


def _ncc(a, b):
    a = a - a.mean(); b = b - b.mean()
    d = (float((a * a).sum()) * float((b * b).sum())) ** 0.5
    return float((a * b).sum() / d) if d else 0.0


def match_template(img, tpl, cx, cy, radius=70):
    """Best normalized cross-correlation of the learned button near (cx, cy). Returns (score, inner, (x, y)):
    `inner` scores only the glyph in the middle, so a send arrow on the same dark disc doesn't pass."""
    th, tw, _ = tpl.shape
    H, W, _ = img.shape
    t = tpl.mean(axis=2)
    g = img.mean(axis=2)
    best, bxy = -1.0, None
    for y in range(max(0, int(cy - radius - th / 2)), min(H - th, int(cy + radius - th / 2)) + 1):
        for x in range(max(0, int(cx - radius - tw / 2)), min(W - tw, int(cx + radius - tw / 2)) + 1, 2):
            sc = _ncc(g[y:y + th, x:x + tw], t)
            if sc > best:
                best, bxy = sc, (x, y)
    if bxy is None:
        return -1.0, -1.0, None
    x, y = bxy
    # refine on odd columns next to the best even one
    for xx in (x - 1, x + 1):
        if 0 <= xx <= W - tw:
            sc = _ncc(g[y:y + th, xx:xx + tw], t)
            if sc > best:
                best, x = sc, xx
    q = th // 4
    inner = _ncc(g[y + q:y + th - q, x + q:x + tw - q], t[q:th - q, q:tw - q])
    return best, inner, (x + tw / 2.0, y + th / 2.0)


def learn_button(img, cx, cy, ww, strip_h):
    x0, y0 = int(round(cx - 18)), int(round(cy - 18))
    tpl = img[y0:y0 + 36, x0:x0 + 36].round().astype(int).tolist()
    write_private(CALIB, json.dumps({"fromRight": ww - cx, "fromBottom": strip_h - cy, "template": tpl, "learned": int(time.time())}))


def start_voice(cfg, agent):
    """Open the agent's chat and press its voice button. Returns a short outcome string."""
    aid = agent.get("agentId", "")
    if not aid or "TODO" in aid:
        notify(f"Hey {agent.get('name')}", "This agent's Grok Bot id isn't set yet (Settings > Voice Wake).")
        return "no-agent-id"
    url = cfg.get("openUrl", DEFAULTS["openUrl"]).replace("{id}", aid)
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not cfg.get("startVoice", True):
        return "opened"
    win = None
    t0 = time.time()
    while time.time() - t0 < 8:
        time.sleep(0.3)
        aw = hypr_json("activewindow") or {}
        if "grok" in (aw.get("class") or "").lower():
            win = aw
            break
    if not win:
        return "no-window"
    time.sleep(cfg.get("settleMs", 1400) / 1000.0)
    if grok_recording():
        return "already-live"
    win = grok_window() or win
    (wx, wy), (ww, wh) = win["at"], win["size"]
    strip_h = min(260, wh)
    sy = wy + wh - strip_h
    shell_ipc("setCapture", "true")
    time.sleep(0.18)
    try:
        img = grab(wx, sy, ww, strip_h)
    finally:
        shell_ipc("setCapture", "false")
    import numpy as np
    target, how = None, ""
    cal = None
    try:
        with open(CALIB) as f:
            cal = json.load(f)
    except Exception:
        pass
    if cal:
        tpl = np.array(cal["template"], dtype=np.float32)
        px, py = ww - cal["fromRight"], strip_h - cal["fromBottom"]
        score, inner, xy = match_template(img, tpl, px, py)
        log(f"voice button match {score:.2f} / glyph {inner:.2f}")
        if xy and score >= 0.85 and inner >= 0.75:
            target, how = xy, f"template {score:.2f}/{inner:.2f}"
        else:
            # Learned once and not found now: a draft in the composer (the send arrow), another theme or an
            # app update. Don't guess; "Forget" in Settings relearns it on the next wake.
            return "button-not-found"
    else:
        xy = find_voice_button(img)
        if xy:
            target, how = xy, "heuristic"
    if target is None:
        return "button-not-found"
    cx, cy = target
    subprocess.run([VCLICK, str(int(wx + cx)), str(int(sy + cy))], timeout=5)
    ok = False
    for _ in range(12):
        time.sleep(0.25)
        if grok_recording():
            ok = True
            break
    if ok and how == "heuristic":
        learn_button(img, cx, cy, ww, strip_h)
    log("voice button:", how, "->", "voice live" if ok else "no mic stream seen")
    return "voice-live" if ok else "clicked-unconfirmed"


# ------------------------------------------------------------------ the listener
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
        if self.cfg.get("overlay", True):
            shell_ipc("setLive", "true")
        if outcome in ("no-window", "button-not-found", "clicked-unconfirmed") or outcome.startswith("error"):
            notify(f"Hey {name}", "Opened the chat. Click the voice button to talk (I couldn't start voice by myself).")


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
    else:
        print(__doc__.strip()); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
