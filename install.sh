#!/usr/bin/env bash
# Voice Wake engine installer (user space, never sudo). Safe to run again.
#
#   ./install-engine.sh [SOURCE_DIR]
#
# SOURCE_DIR holds engine/, plugin/ and systemd/ (default: this script's directory).
# Installs into ~/.local/share/oma-voice-wake: a Python 3.12 venv (uv) with Vosk + Piper,
# the Vosk small English model (~40 MB download), an offline Piper voice for the prompts,
# the oma-vclick helper (Wayland virtual pointer; no root, no uinput), the shell plugin and
# the oma-voice-wake.service user unit. Changed files are backed up as <file>.bak-<epoch>.
set -euo pipefail
SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
D="$HOME/.local/share/oma-voice-wake"
PLUG="$HOME/.config/omarchy/plugins/johnatfreecoffee.voice-wake"
TS="$(date +%s)"
MODEL_URL="https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low"
say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
put() { # put SRC DEST [MODE]
  local s="$1" d="$2" m="${3:-644}"
  [[ -e $d ]] && cmp -s "$s" "$d" && return 0
  mkdir -p "$(dirname "$d")"
  [[ -e $d ]] && cp -p -- "$d" "$d.bak-$TS"
  install -m "$m" -- "$s" "$d"
}

say "Voice Wake: checking requirements"
missing=()
for c in pw-record pw-play grim gcc wayland-scanner curl unzip magick hyprctl omarchy-shell nmcli notify-send; do
  command -v "$c" >/dev/null || missing+=("$c")
done
pkg-config --exists wayland-client 2>/dev/null || missing+=("wayland (pkg-config wayland-client)")
command -v uv >/dev/null || missing+=("uv")
if ((${#missing[@]})); then
  warn "Missing: ${missing[*]}"
  warn "Install them yourself, e.g.: sudo pacman -S --needed pipewire grim gcc wayland curl unzip imagemagick libnotify uv pkgconf"
  exit 1
fi

say "Engine files -> $D"
mkdir -p "$D/bin" "$D/src" "$D/models" "$D/voices" "$D/prompts"
put "$SRC/engine/oma_voice_wake.py" "$D/oma_voice_wake.py" 644
put "$SRC/engine/oma-vclick.c" "$D/src/oma-vclick.c" 644
put "$SRC/engine/wlr-virtual-pointer-unstable-v1.xml" "$D/src/wlr-virtual-pointer-unstable-v1.xml" 644
put "$SRC/engine/oma-voice-wake" "$HOME/.local/bin/oma-voice-wake" 755

if [[ ! -x $D/venv/bin/python ]]; then
  say "Python 3.12 venv (uv)"
  uv venv -q --python 3.12 "$D/venv"
fi
VIRTUAL_ENV="$D/venv" uv pip install -q "vosk==0.3.45" "piper-tts>=1.3" numpy

if [[ ! -d $D/models/vosk-model-small-en-us-0.15 ]]; then
  say "Vosk small English model"
  tmp="$(mktemp -d)"; curl -fsSL -o "$tmp/m.zip" "$MODEL_URL"; unzip -q "$tmp/m.zip" -d "$D/models"; rm -rf "$tmp"
fi
for f in en_US-amy-low.onnx en_US-amy-low.onnx.json; do
  [[ -s $D/voices/$f ]] || { say "Piper voice $f"; curl -fsSL -o "$D/voices/$f" "$VOICE_URL/$f"; }
done

say "Spoken prompts (offline Piper voice)"
while IFS='|' read -r key text; do
  [[ -s $D/prompts/$key.wav ]] || printf '%s\n' "$text" | "$D/venv/bin/piper" -m "$D/voices/en_US-amy-low.onnx" -f "$D/prompts/$key.wav" >/dev/null 2>&1
done <<'P'
ask|What's your voice password?
retry|Sorry, try again.
unlocked|Unlocked.
locked_out|Voice unlock is paused. Please type your password.
untrusted|Voice unlock only works on trusted Wi-Fi. Please type your password.
typed_first|Please type your password once first.
listening|Listening.
P

say "oma-vclick (virtual pointer click helper)"
( cd "$D/src"
  wayland-scanner client-header wlr-virtual-pointer-unstable-v1.xml wlr-virtual-pointer-unstable-v1-client-protocol.h
  wayland-scanner private-code wlr-virtual-pointer-unstable-v1.xml wlr-virtual-pointer-protocol.c
  gcc -O2 -o "$D/bin/oma-vclick.new" oma-vclick.c wlr-virtual-pointer-protocol.c $(pkg-config --cflags --libs wayland-client) )
if [[ -x $D/bin/oma-vclick ]] && cmp -s "$D/bin/oma-vclick.new" "$D/bin/oma-vclick"; then rm -f "$D/bin/oma-vclick.new"; else mv -f "$D/bin/oma-vclick.new" "$D/bin/oma-vclick"; fi

say "Shell plugin -> $PLUG"
put "$SRC/plugin/manifest.json" "$PLUG/manifest.json" 644
put "$SRC/plugin/Service.qml" "$PLUG/Service.qml" 644
ICON=/usr/share/icons/hicolor/512x512/apps/grok-bot.png
if [[ -e $ICON ]]; then
  tmp="$(mktemp --suffix=.png)"
  # The official icon is a cropped close-up; make-head.py fills the missing
  # chin and left side so the whole head sits inside the white disc.
  if "$D/venv/bin/python" "$SRC/plugin/make-head.py" "$ICON" "$tmp"; then
    put "$tmp" "$PLUG/head.png" 644
  else
    warn "could not rebuild head.png from $ICON; leaving any existing head in place."
  fi
  rm -f "$tmp"
else
  warn "Grok Bot app icon not found ($ICON): the overlay shows an empty white circle."
fi

# Config: created once with no agents (add them in Settings > Voice Wake, or edit the JSON).
CFG="$HOME/.config/oma/voice-wake.json"
if [[ ! -e $CFG ]]; then
  mkdir -p "$(dirname "$CFG")"
  ( umask 077; cp "$SRC/engine/voice-wake.example.json" "$CFG" )
fi
chmod 600 "$CFG"

say "Enabling the plugin in the Omarchy shell"
omarchy-shell -q shell rescanPlugins || true
omarchy-plugin-enable johnatfreecoffee.voice-wake >/dev/null 2>&1 || warn "enable it yourself: omarchy plugin enable johnatfreecoffee.voice-wake"

say "systemd --user unit"
put "$SRC/systemd/oma-voice-wake.service" "$HOME/.config/systemd/user/oma-voice-wake.service" 644
systemctl --user daemon-reload
systemctl --user enable oma-voice-wake.service >/dev/null 2>&1 || warn "could not enable oma-voice-wake.service"
systemctl --user restart oma-voice-wake.service || warn "oma-voice-wake.service did not start (journalctl --user -u oma-voice-wake)"

say "Voice Wake engine installed."
echo "   Add agents and set a voice passcode: Settings > Voice Wake (or edit $CFG; oma-voice-wake passcode set)."
echo "   Status: oma-voice-wake status"
