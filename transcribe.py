#!/usr/bin/env python3
"""consumer transcribe — local video/audio -> word-timestamped transcript.

Provenance: MLX Whisper pattern from the MediaPlural refurbapp ETL
(pinned HF revision, isolated venv, JSON bridge subprocess, sanitized env).
Verified live 2026-10-06: 15.7s test audio -> exact
transcript in ~3s, word timestamps intact.

Backend selection:
  mlx    (default on Apple Silicon)  mlx-community/whisper-large-v3-turbo @ pinned revision
  faster (default elsewhere)          Systran faster-whisper via CTranslate2

Outputs (per input, in --out-dir):
  <stem>.transcript.json  full whisper result (text, language, segments, words)
  <stem>.txt              plain text
  <stem>.srt              subtitles from segments
  <stem>.sha256           source-file fingerprint (provenance)

Env notes (earned the hard way, do not "fix"):
  - The child interpreter runs with PYTHONPATH/VIRTUAL_ENV scrubbed: host
    kernels leak their site-packages into subprocesses and break the venv.
  - ffmpeg MUST be on the child PATH (whisper shells out for audio decode).
"""
import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys

MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
MLX_REVISION = "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
FASTER_MODEL = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"

MLX_BRIDGE = r'''
import json, math, sys, mlx_whisper
from huggingface_hub import snapshot_download
def norm(v):
    if isinstance(v, float) and not math.isfinite(v): return None
    if isinstance(v, dict): return {k: norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)): return [norm(x) for x in v]
    return v
video, model, revision, language, words = sys.argv[1:6]
path = snapshot_download(repo_id=model, revision=revision)
r = mlx_whisper.transcribe(video, path_or_hf_repo=path,
                           language=(language or None), word_timestamps=(words == "1"))
print(json.dumps(norm(r), ensure_ascii=False, allow_nan=False))
'''

FASTER_BRIDGE = r'''
import json, sys
from faster_whisper import WhisperModel
video, model, language, words = sys.argv[1:5]
m = WhisperModel(model, device="auto", compute_type="auto")
segments, info = m.transcribe(video, language=(language or None), word_timestamps=(words == "1"))
out = {"text": "", "language": info.language, "segments": []}
for s in segments:
    seg = {"start": s.start, "end": s.end, "text": s.text}
    if s.words:
        seg["words"] = [{"word": w.word, "start": w.start, "end": w.end} for w in s.words]
    out["segments"].append(seg)
    out["text"] += s.text
print(json.dumps(out, ensure_ascii=False))
'''

FFMPEG_CANDIDATES = [
    shutil.which("ffmpeg") or "",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    os.path.expanduser("~/.hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg"),
    os.path.expanduser("~/bin/ffmpeg"),
    "/usr/bin/ffmpeg",
]


def find_ffmpeg():
    for c in FFMPEG_CANDIDATES:
        if c and os.path.exists(c):
            return os.path.dirname(c)
    sys.exit("FATAL: ffmpeg not found — install it or add its dir to PATH")


def child_env(venv_bin):
    """Scrubbed environment for the bridge interpreter."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTHON", "VIRTUAL_ENV", "CONDA"))}
    env["PATH"] = find_ffmpeg() + ":" + venv_bin + ":/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def srt_time(t):
    ms = int(round(max(0.0, float(t)) * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments, path):
    lines = []
    for i, seg in enumerate(segments, 1):
        text = seg.get("text", "").strip()
        if not text:
            continue
        lines.append(f"{i}\n{srt_time(seg['start'])} --> {srt_time(seg['end'])}\n{text}\n")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_backend(requested):
    if requested != "auto":
        return requested
    return "mlx" if platform.machine() == "arm64" and platform.system() == "Darwin" else "faster"


def transcribe(src, venv_python, backend, language, word_ts, timeout_s, out_dir):
    stem = os.path.splitext(os.path.basename(src))[0]
    os.makedirs(out_dir, exist_ok=True)
    venv_bin = os.path.dirname(venv_python)

    if backend == "mlx":
        cmd = [venv_python, "-c", MLX_BRIDGE, src, MLX_MODEL, MLX_REVISION, language, "1" if word_ts else "0"]
    else:
        cmd = [venv_python, "-c", FASTER_BRIDGE, src, FASTER_MODEL, language, "1" if word_ts else "0"]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s,
                          env=child_env(venv_bin))
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-1200:]
        sys.exit(f"FATAL: {backend} bridge failed (exit {proc.returncode}):\n{tail}")

    result = json.loads(proc.stdout)
    jpath = os.path.join(out_dir, f"{stem}.transcript.json")
    with open(jpath, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, f"{stem}.txt"), "w") as f:
        f.write(result.get("text", "").strip() + "\n")
    write_srt(result.get("segments", []), os.path.join(out_dir, f"{stem}.srt"))
    with open(os.path.join(out_dir, f"{stem}.sha256"), "w") as f:
        f.write(f"{sha256_file(src)}  {os.path.abspath(src)}\n")

    n_words = sum(len(s.get("words", [])) for s in result.get("segments", []))
    dur = result.get("segments", [{}])[-1].get("end", 0) if result.get("segments") else 0
    return {"json": jpath, "language": result.get("language"),
            "segments": len(result.get("segments", [])), "words": n_words,
            "duration_s": round(dur, 2), "sha256": sha256_file(src)}


def main():
    ap = argparse.ArgumentParser(description="Local transcription: video/audio -> transcript JSON/TXT/SRT")
    ap.add_argument("source", help="input video or audio file")
    ap.add_argument("--out-dir", default=None, help="output directory (default: alongside source)")
    ap.add_argument("--venv-python", default=None, help="isolated interpreter (see install.sh)")
    ap.add_argument("--backend", choices=["auto", "mlx", "faster"], default="auto")
    ap.add_argument("--language", default="en", help="ISO code, or 'auto' to detect")
    ap.add_argument("--no-word-timestamps", action="store_true")
    ap.add_argument("--timeout", type=int, default=18000, help="per-file seconds (default 5h)")
    a = ap.parse_args()

    venv = a.venv_python or os.environ.get(
        "CONSUMER_VENV_PYTHON",
        os.path.expanduser("~/.hermes/venvs/consumer/bin/python"))
    if not os.path.exists(venv):
        sys.exit(f"FATAL: interpreter not found: {venv}\nRun install.sh first.")
    if not os.path.exists(a.source):
        sys.exit(f"FATAL: no such file: {a.source}")

    backend = pick_backend(a.backend)
    out_dir = a.out_dir or os.path.join(os.path.dirname(os.path.abspath(a.source)), "transcripts")
    stats = transcribe(a.source, venv, backend, a.language if a.language != "auto" else None,
                       not a.no_word_timestamps, a.timeout, out_dir)
    print(json.dumps({"ok": True, "backend": backend, **stats}, indent=2))


if __name__ == "__main__":
    main()