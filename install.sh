#!/usr/bin/env sh
# ===================================================
# CFD Installer (Linux / macOS)
#
# Copies cfd.sh and cfd-core.py to a permanent folder,
# makes them executable, and adds that folder to your
# PATH (in your shell profile).
#
# Run this once:  sh install.sh
# ===================================================

INSTALL_DIR="${CFD_INSTALL_DIR:-$HOME/.local/bin}"
HERE="$(dirname "$0")"
LAUNCHER="$HERE/cfd.sh"
CORE="$HERE/cfd-core.py"

echo "==================================================="
echo "CFD - Current Folder Downloader - Installer"
echo "==================================================="
echo

if [ ! -f "$LAUNCHER" ]; then
    echo "[Error] cfd.sh was not found next to install.sh."
    exit 1
fi
if [ ! -f "$CORE" ]; then
    echo "[Error] cfd-core.py was not found next to install.sh."
    exit 1
fi

PY=""
for p in python3 python; do
    if command -v "$p" >/dev/null 2>&1; then
        PY="$p"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[Error] Python 3 is required but was not found on PATH."
    exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
    echo "[Warning] curl is not on PATH. Direct-URL downloads will fail."
fi

mkdir -p "$INSTALL_DIR"
cp "$LAUNCHER" "$INSTALL_DIR/cfd"
cp "$CORE" "$INSTALL_DIR/cfd-core.py"
chmod +x "$INSTALL_DIR/cfd"

echo "Installed cfd to $INSTALL_DIR"
echo

ON_PATH=0
for d in $(echo "$PATH" | tr ':' ' '); do
    if [ "${d%/}" = "${INSTALL_DIR%/}" ]; then
        ON_PATH=1
        break
    fi
done

if [ "$ON_PATH" = "1" ]; then
    echo "$INSTALL_DIR is already on your PATH - you can use cfd right now."
else
    case "$SHELL" in
        *zsh)  PROFILE="$HOME/.zshrc" ;;
        *bash) PROFILE="$HOME/.bashrc" ;;
        *)     PROFILE="$HOME/.profile" ;;
    esac

    LINE="export PATH=\"$INSTALL_DIR\":\$PATH"
    if grep -F "$LINE" "$PROFILE" >/dev/null 2>&1; then
        echo "$INSTALL_DIR is already exported in $PROFILE."
    else
        printf '\n# CFD - Current Folder Downloader\n%s\n' "$LINE" >> "$PROFILE"
        echo "Added $INSTALL_DIR to $PROFILE"
    fi
    echo
    echo "Reload your shell (or run):  export PATH=\"$INSTALL_DIR\":\$PATH"
fi

echo
echo "==================================================="
echo "Install complete! Try it from any folder:"
echo "  cfd hf download hf://prism-ml/Bonsai-27B-gguf/Bonsai-27B-Q1_0.gguf"
echo "  cfd https://huggingface.co/.../resolve/main/file.gguf?download=true"
echo
echo "To remove it later:  cfd uninstall"
echo "==================================================="
