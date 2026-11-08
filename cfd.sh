#!/usr/bin/env sh
# ===================================================
# CFD - Current Folder Downloader  (Linux launcher)
#
# All the logic lives in cfd-core.py. This file only
# finds Python 3, remembers its own path, and passes
# every argument through to the core. Keep it in the
# same folder as cfd-core.py.
#
# Usage:
#   cfd hf download hf://prism-ml/Bonsai-27B-gguf/Bonsai-27B-Q1_0.gguf
#   cfd https://huggingface.co/.../resolve/main/file.gguf?download=true
#   cfd status          - show active/current/queued downloads here
#   cfd continue / c    - resume using saved queue data
#   cfd clear           - wipe the pending queue (and in-progress marker) here
#   cfd log             - show the tail of the worker log (background mode)
#   cfd uninstall       - remove cfd
#
# Env overrides (mainly for tests):
#   CFD_SMALL_DELAY         seconds between retries (default 10)
#   CFD_WORKER_EXIT_DELAY   seconds to linger after the queue empties (default 5)
#   CFD_TERMINAL            command used to open the worker in its own window
#   CFD_WORKER_MODE         window | background | inline
# ===================================================

# Resolve our own absolute path so `cfd uninstall` knows where it lives.
CFD_LAUNCHER="$0"
if command -v readlink >/dev/null 2>&1; then
    _resolved="$(readlink -f "$0" 2>/dev/null)"
    if [ -n "$_resolved" ]; then
        CFD_LAUNCHER="$_resolved"
    fi
fi
export CFD_LAUNCHER

CORE="$(dirname "$CFD_LAUNCHER")/cfd-core.py"

if [ ! -f "$CORE" ]; then
    echo "[Error] cfd-core.py is missing - keep it in the same folder as cfd."
    echo "Looked at: $CORE"
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

exec "$PY" "$CORE" "$@"
