#!/bin/bash
# Double-click to start Home Finance on a Mac. The first run downloads what it needs
# (the "uv" tool and Python, about 50 MB) and can take a minute; later starts are quick.
cd "$(dirname "$0")" || exit 1

UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
  echo "First run: installing uv (it fetches Python for Home Finance)..."
  if ! curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh; then
    echo; echo "Couldn't download uv. Check your internet connection and try again."
    read -r -p "Press Return to close." _; exit 1
  fi
  UV="$HOME/.local/bin/uv"
fi

"$UV" run --quiet --no-project --python 3.13 --with-requirements requirements.txt python run.py "$@"
status=$?
if [ $status -ne 0 ]; then
  echo; echo "Home Finance stopped with an error (code $status)."
  read -r -p "Press Return to close." _
fi
exit $status
