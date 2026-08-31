#!/usr/bin/env bash
# Rebuild the stylesheet after editing anything in app/templates/.
#   ./build-css.sh          one-off build
#   ./build-css.sh --watch  rebuild automatically while developing
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x bin/tailwindcss ]; then
  echo "bin/tailwindcss not found. Download it (no Node required):" >&2
  echo "  see the 'Styling' section of README.md" >&2
  exit 1
fi

exec ./bin/tailwindcss \
  -i app/static/src.css \
  -o app/static/tailwind.css \
  --minify "$@"
