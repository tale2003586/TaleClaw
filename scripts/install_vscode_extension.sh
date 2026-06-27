#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTENSION_SRC="$ROOT_DIR/vscode-extension"
EXTENSION_NAME="taleclaw-coding-agent"
EXTENSIONS_DIR="${VSCODE_EXTENSIONS_DIR:-$HOME/.vscode/extensions}"
EXTENSION_DEST="$EXTENSIONS_DIR/$EXTENSION_NAME"

if [[ ! -f "$EXTENSION_SRC/package.json" ]]; then
  echo "Extension source not found: $EXTENSION_SRC" >&2
  exit 1
fi

mkdir -p "$EXTENSIONS_DIR"

if [[ -L "$EXTENSION_DEST" ]]; then
  rm "$EXTENSION_DEST"
elif [[ -e "$EXTENSION_DEST" ]]; then
  echo "Refusing to overwrite existing non-symlink extension directory:" >&2
  echo "  $EXTENSION_DEST" >&2
  echo "Move it away or set VSCODE_EXTENSIONS_DIR to another extensions directory." >&2
  exit 1
fi

ln -s "$EXTENSION_SRC" "$EXTENSION_DEST"

echo "Installed TaleClaw VS Code extension as a local symlink:"
echo "  $EXTENSION_DEST -> $EXTENSION_SRC"
echo
echo "Reload VS Code, then open the TaleClaw activity bar view."
