#!/bin/bash
# Herdr Voice for macOS: builds HerdrVoice.app on this Mac and installs it into ~/Applications.
# Usage:  curl -fsSL https://raw.githubusercontent.com/Tenzen1231/Speak-Russian-Herdr-Voicer/main/mac/install.sh | bash
#    or:  bash mac/install.sh            (from a downloaded copy of the repository)
# Flags:  --no-launch --no-autostart     (used by CI)

# Everything lives in main() so a partially downloaded script never runs.
main() {
set -Eeuo pipefail

local REPO="Tenzen1231/Speak-Russian-Herdr-Voicer"
local BRANCH="${HERDR_BRANCH:-main}"
local MODEL_URL="https://huggingface.co/istupakov/gigaam-v3-onnx/resolve/322c3b29492673eb7d0b434bfa9dfb8653e34d02"
local APP_DIR="$HOME/Applications"
local APP="$APP_DIR/HerdrVoice.app"
local WORK="$HOME/Library/Caches/HerdrVoice-build"
local MODEL_CACHE="$HOME/Library/Caches/HerdrVoice-model"
local AGENT="$HOME/Library/LaunchAgents/com.herdr.voice.plist"
local LAUNCH=1 AUTOSTART=1
for arg in "$@"; do
  case "$arg" in
    --no-launch) LAUNCH=0 ;;
    --no-autostart) AUTOSTART=0 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }
trap 'fail "установка прервалась (строка $LINENO). Пришлите скриншот этого окна."' ERR

[ "$(uname -s)" = Darwin ] || fail "этот установщик только для macOS."
local major
major=$(sw_vers -productVersion | cut -d. -f1)
[ "$major" -ge 12 ] || fail "нужна macOS 12 Monterey или новее."

printf '\033[1mHerdr Voice: русская диктовка для macOS\033[0m\n'
printf 'Займёт 3-5 минут и около 1 ГБ трафика. Права администратора не нужны.\n'

# 1. Sources: this checkout, or a fresh download when piped from curl.
step "Готовлю исходники"
rm -rf "$WORK"
mkdir -p "$WORK/src"
local here=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
if [ -n "$here" ] && [ -f "$here/core/dictate.py" ]; then
  rsync -a --exclude .git --exclude 'core/gigaam' --exclude venv --exclude dist --exclude build \
    --exclude __pycache__ "$here/" "$WORK/src/"
else
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" \
    | tar -xz -C "$WORK/src" --strip-components 1
fi

# 2. Python and libraries through uv: a single binary in ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  step "Ставлю менеджер Python (uv)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
step "Ставлю Python и библиотеки"
uv venv --quiet --python 3.12 "$WORK/venv"
uv pip install --quiet --python "$WORK/venv/bin/python" -r "$WORK/src/mac/requirements.txt"

# 3. GigaAM v3 model, pinned revision, verified by SHA-256.
step "Скачиваю модель распознавания GigaAM v3 (около 230 МБ)"
mkdir -p "$MODEL_CACHE" "$WORK/src/core/gigaam"
local name sum
while read -r name sum; do
  if ! echo "$sum  $MODEL_CACHE/$name" | shasum -a 256 -c --status 2>/dev/null; then
    curl -fL --retry 3 --progress-bar -o "$MODEL_CACHE/$name" "$MODEL_URL/$name"
    echo "$sum  $MODEL_CACHE/$name" | shasum -a 256 -c --status \
      || fail "файл модели $name скачался с ошибкой. Запустите установку ещё раз."
  fi
  cp "$MODEL_CACHE/$name" "$WORK/src/core/gigaam/$name"
done <<'EOF'
config.json 0641fcf73f4af791c73f05083e38a658ff5dcbee3534a0c61396c10f4b97f0fe
v3_e2e_rnnt.yaml d23270a1f327e365ebc4554b7ceee69ac12898e21092c18085151516aa58f91b
v3_e2e_rnnt_vocab.txt 39abae20e692998290c574e606f11a9edef2902a1995463fcff63d1490cf22b7
v3_e2e_rnnt_decoder.int8.onnx 89014e134865615b91e037157e46e389b1271e6072460efc010ea08e61e23146
v3_e2e_rnnt_joint.int8.onnx ade116563dbf66e503b0994efab6b5861412743e52bf31c39fc3fffa3783d5d1
v3_e2e_rnnt_encoder.int8.onnx 4e0e076a6076cd110277e529b8ac8f32cd5297f7fbebad5341ae8ddb7d00817b
EOF

# 4. Build the application bundle on this Mac (native architecture, ad-hoc signed).
step "Собираю HerdrVoice.app"
(cd "$WORK/src" && "$WORK/venv/bin/python" -m PyInstaller --noconfirm --log-level WARN \
  --distpath "$WORK/dist" --workpath "$WORK/build" mac/HerdrVoice-mac.spec)

# 5. Install. A rebuilt app has a new signature, so stale permissions are reset.
step "Устанавливаю в $APP_DIR"
local reinstall=0
[ -d "$APP" ] && reinstall=1
pkill -x HerdrVoice 2>/dev/null && sleep 1 || true
mkdir -p "$APP_DIR"
rm -rf "$APP"
ditto "$WORK/dist/HerdrVoice.app" "$APP"
if [ "$reinstall" = 1 ]; then
  tccutil reset Accessibility com.herdr.voice >/dev/null 2>&1 || true
  tccutil reset Microphone com.herdr.voice >/dev/null 2>&1 || true
fi

if [ "$AUTOSTART" = 1 ]; then
  mkdir -p "$(dirname "$AGENT")"
  cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.herdr.voice</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/open</string><string>-a</string><string>$APP</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST
fi

rm -rf "$WORK"

if [ "$LAUNCH" = 1 ]; then
  open "$APP"
fi

trap - ERR
printf '\n\033[1;32mГотово. Herdr Voice установлен.\033[0m\n\n'
printf 'Осталось два разрешения (macOS спросит сама):\n'
printf '  1. Микрофон: нажмите «Разрешить».\n'
printf '  2. Универсальный доступ: включите переключатель у HerdrVoice\n'
printf '     в Системных настройках. Без него не работают клавиша и вставка текста.\n\n'
printf 'Диктовка: нажмите и отпустите правый Option, говорите, нажмите ещё раз.\n'
printf 'Значок микрофона в строке меню сверху справа: настройки и выход.\n'
}

main "$@"
