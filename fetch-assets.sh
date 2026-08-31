#!/usr/bin/env bash
# Download the third-party front-end assets this app serves locally, plus the
# Tailwind standalone CLI (a single binary — no Node or npm required).
#
# Everything lands in app/static/ and bin/, both gitignored, so the repo stays
# small and these stay reproducible. Run once after cloning:
#
#   ./fetch-assets.sh && ./build-css.sh
set -euo pipefail
cd "$(dirname "$0")"

KEYRUNE="https://cdn.jsdelivr.net/npm/keyrune@latest"
MANA="https://cdn.jsdelivr.net/npm/mana-font@latest"
DAISYUI="https://cdn.jsdelivr.net/npm/daisyui@4/dist/full.min.css"
HTMX="https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js"
OPENCV="https://docs.opencv.org/4.x/opencv.js"

mkdir -p app/static/fonts bin

# --- Tailwind standalone CLI -------------------------------------------------
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) TW_ASSET="tailwindcss-macos-arm64" ;;
  Darwin-x86_64) TW_ASSET="tailwindcss-macos-x64" ;;
  Linux-x86_64) TW_ASSET="tailwindcss-linux-x64" ;;
  Linux-aarch64) TW_ASSET="tailwindcss-linux-arm64" ;;
  *) echo "Unsupported platform for the Tailwind CLI: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

if [ ! -x bin/tailwindcss ]; then
  echo "Downloading Tailwind standalone CLI ($TW_ASSET)..."
  curl -sL --fail -o bin/tailwindcss \
    "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/$TW_ASSET"
  chmod +x bin/tailwindcss
fi

# --- Stylesheets and scripts -------------------------------------------------
echo "Downloading DaisyUI, Keyrune, Mana, and htmx..."
curl -sL --fail -o app/static/daisyui.css "$DAISYUI"
curl -sL --fail -o app/static/htmx.min.js "$HTMX"
echo "Downloading OpenCV.js (~10MB, used for live card detection)..."
curl -sL --fail -o app/static/opencv.js "$OPENCV"
curl -sL --fail -o app/static/keyrune.css "$KEYRUNE/css/keyrune.css"
curl -sL --fail -o app/static/mana.css "$MANA/css/mana.css"
for f in keyrune.woff2 keyrune.woff keyrune.ttf; do
  curl -sL --fail -o "app/static/fonts/$f" "$KEYRUNE/fonts/$f"
done
for f in mana.woff2 mana.woff mana.ttf; do
  curl -sL --fail -o "app/static/fonts/$f" "$MANA/fonts/$f"
done

# Both fonts' CSS points at ../fonts/; we serve them under /static/fonts/.
python3 - <<'PY'
import pathlib
for name in ("keyrune.css", "mana.css"):
    p = pathlib.Path("app/static") / name
    p.write_text(p.read_text().replace("../fonts/", "/static/fonts/"))
PY

echo "Done. Now run ./build-css.sh to compile app/static/tailwind.css"
