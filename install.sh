#!/usr/bin/env bash
# consumer install — isolated transcription + acquisition venv (no system pollution).
# macOS/Apple Silicon: mlx-whisper (GPU via MLX). Linux/other: faster-whisper (CPU).
# Usage: bash install.sh [venv-dir]   (default ~/.hermes/venvs/bradan)
set -euo pipefail

VENV_DIR="${1:-$HOME/.hermes/venvs/bradan}"
PY_MINOR=3.11

say() { printf '[install] %s\n' "$*"; }

# --- locate uv (preferred) else fall back to python3 -m venv ---
UV=""
for c in "$HOME/.hermes/bin/uv" "$(command -v uv || true)" "$HOME/.local/bin/uv"; do
  [ -x "$c" ] && UV="$c" && break
done

if [ -n "$UV" ]; then
  say "creating venv at $VENV_DIR with uv (python 3.$PY_MINOR)"
  "$UV" venv "$VENV_DIR" --python "$PY_MINOR" >/dev/null
  PIP=( "$UV" pip install --python "$VENV_DIR/bin/python" )
else
  say "uv not found; using python3 -m venv"
  python3 -m venv "$VENV_DIR"
  PIP=( "$VENV_DIR/bin/pip" install )
fi

PY="$VENV_DIR/bin/python"

case "$(uname -s)/$(uname -m)" in
  Darwin/arm64)
    say "Apple Silicon: installing mlx-whisper==0.4.3 (pinned)"
    "${PIP[@]}" 'mlx-whisper==0.4.3'
    BACKEND=mlx
    ;;
  *)
    say "$(uname -s): installing faster-whisper"
    "${PIP[@]}" 'faster-whisper' 'ctranslate2'
    BACKEND=faster
    ;;
esac

say "installing acquisition tooling (yt-dlp, huggingface-hub)"
"${PIP[@]}" 'yt-dlp'

# --- ffmpeg check (whisper shells out to it for decode) ---
if ! command -v ffmpeg >/dev/null 2>&1; then
  for c in "$HOME/.hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg" "$HOME/bin/ffmpeg"; do
    [ -x "$c" ] && say "note: ffmpeg found at $c (transcribe.py will pick it up)" && break
  done || say "WARNING: no ffmpeg on PATH — transcription will fail until ffmpeg is installed"
fi

# --- verify import with a scrubbed env (host PYTHONPATH leaks break the venv) ---
if env -u PYTHONPATH -u VIRTUAL_ENV "$PY" -c "import mlx_whisper" 2>/dev/null \
   || env -u PYTHONPATH -u VIRTUAL_ENV "$PY" -c "import faster_whisper" 2>/dev/null; then
  say "OK: STT import verified in isolation"
else
  say "FATAL: STT import failed in isolation"; exit 1
fi

cat <<EOF

DONE. Backend: $BACKEND
Set it for every run:
  export BRADAN_VENV_PYTHON="$VENV_DIR/bin/python"
or pass --venv-python per call. transcribe.py picks the backend automatically.

Quick test:
  "$VENV_DIR/bin/python" "$(dirname "$0")/transcribe.py" --help
EOF