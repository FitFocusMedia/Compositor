#!/bin/zsh
# Sets the toolkit up on this Mac, and is safe to run again: builds comp-render when the app's sources are newer
# than it, makes the Python environment, writes the comp-render / compkit / compkit-python commands into
# ~/.local/bin, and links each skill in skills/ into ~/.claude/skills (a skill folder already there that isn't
# one of these links is left alone).
#   automation/install.sh [--rebuild]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
BIN="$HOME/.local/bin"
SKILLS="$HOME/.claude/skills"

if [[ "${1:-}" == "--rebuild" || ! -x "$ROOT/bin/comp-render" ]] ||
   [[ -n "$(find "$REPO/Compositor" "$ROOT/comp-render" -newer "$ROOT/bin/comp-render" \
             \( -name '*.swift' -o -name '*.c' -o -name '*.h' \) -print -quit)" ]]; then
  "$ROOT/comp-render/build.sh"
else
  echo "==> comp-render is up to date"
fi

echo "==> Python environment"
[[ -x "$ROOT/.venv/bin/python" ]] || python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q --disable-pip-version-check -r "$ROOT/requirements.txt"

echo "==> Commands in $BIN"
mkdir -p "$BIN"
ln -sf "$ROOT/bin/comp-render" "$BIN/comp-render"
for name in compkit compkit-python; do
  run='"$@"'; [[ $name == compkit ]] && run='-m compkit "$@"'
  cat > "$BIN/$name" <<LAUNCHER
#!/bin/sh
# $name, from the Compositor automation toolkit (written by $ROOT/install.sh)
ROOT='$ROOT'
PYTHONPATH="\$ROOT\${PYTHONPATH:+:\$PYTHONPATH}" exec "\$ROOT/.venv/bin/python" $run
LAUNCHER
  chmod +x "$BIN/$name"
done
[[ ":$PATH:" == *":$BIN:"* ]] || echo "    note: $BIN isn't on PATH; add it to ~/.zprofile to use these commands by name"

echo "==> Skills in $SKILLS"
mkdir -p "$SKILLS"
for skill in "$ROOT"/skills/*(/); do
  target="$SKILLS/${skill:t}"
  if [[ -e "$target" && ! -L "$target" ]]; then
    echo "    skipped ${skill:t}: $target exists and isn't a link"
    continue
  fi
  ln -sfn "$skill" "$target"
  echo "    ${skill:t}"
done

"$BIN/comp-render" defaults > /dev/null && "$BIN/compkit" where > /dev/null && echo "==> Ready"
