#!/usr/bin/env python3
"""consumer — one-command wrapper: acquire → transcribe → distill.

Usage:
  python3 consumer.py "https://youtube.com/watch?v=XXX" --tag my-course
  python3 consumer.py ./course/media --tag my-course        (skip acquire; transcribe a dir)
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd, **kw):
    print(f"[consumer] $ {' '.join(cmd)}", file=sys.stderr)
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        sys.exit(f"FATAL: step failed: {' '.join(cmd)}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="URL (acquire first) or directory of media files (skip acquire)")
    ap.add_argument("--tag", default="course", help="workspace name under ./consumer-out/ (default: course)")
    ap.add_argument("--audio-only", action="store_true", default=True)
    ap.add_argument("--keep-video", dest="audio_only", action="store_false")
    ap.add_argument("--venv-python", default=os.environ.get(
        "BRADAN_VENV_PYTHON", os.path.expanduser("~/.hermes/venvs/bradan/bin/python")))
    a = ap.parse_args()

    root = os.path.abspath(os.path.join("consumer-out", a.tag))
    media = os.path.join(root, "media")
    transcripts = os.path.join(root, "transcripts")
    distilled = os.path.join(root, "distilled")
    os.makedirs(media, exist_ok=True)

    if a.target.startswith("http"):
        run(["python3", os.path.join(HERE, "scrape.py"), a.target,
             "--out-dir", media, "--venv-python", a.venv_python]
            + (["--audio-only"] if a.audio_only else []))
    else:
        media = os.path.abspath(a.target)

    files = [os.path.join(media, f) for f in sorted(os.listdir(media))
             if f.lower().endswith((".mp4", ".m4a", ".mp3", ".wav", ".aac", ".flac", ".mov", ".mkv", ".webm", ".aiff"))]
    if not files:
        sys.exit(f"FATAL: no media files in {media}")

    for f in files:
        run(["python3", os.path.join(HERE, "transcribe.py"), f,
             "--out-dir", transcripts, "--venv-python", a.venv_python])

    run(["python3", os.path.join(HERE, "distill.py"), transcripts, "--out-dir", distilled])
    print(f"\n[consumer] DONE. Distilled corpus at {distilled}")
    print("[consumer] Next: assimilate via HANDBOOK.md adapters, or drive via INGEST.md/MCP.")


if __name__ == "__main__":
    main()