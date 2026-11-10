#!/bin/bash
# Build "Home Finance.app" and a .dmg installer in dist/. Run on a Mac.
# The app is built for this Mac's chip (Apple silicon or Intel).
set -e
cd "$(dirname "$0")"

UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
  echo "Installing uv (fetches Python for the build)..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  UV="$HOME/.local/bin/uv"
fi

[ -x .build-venv/bin/python ] || "$UV" venv --quiet --clear --python 3.13 .build-venv
"$UV" pip install --quiet --python .build-venv/bin/python -r requirements.txt -r requirements-desktop.txt
.build-venv/bin/pyinstaller --noconfirm --clean --log-level WARN HomeFinance.spec

ARCH="$(uname -m)"; [ "$ARCH" = arm64 ] && ARCH="AppleSilicon"; [ "$ARCH" = x86_64 ] && ARCH="Intel"
DMG="dist/HomeFinance-Mac-$ARCH.dmg"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
cp -R "dist/Home Finance.app" "$STAGE/"
cp "Read Me First.txt" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname "Home Finance" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
echo "Built dist/Home Finance.app and $DMG"
