#!/usr/bin/env python3
"""consumer watch — a drop folder that assimilates itself.

Point it at a directory; it watches (polling, stdlib) and every new/changed
file flows through the whole engine automatically:

  file appears -> ingest (any format) -> OCR flagged images -> transcribe
  flagged media -> graph (idempotent: re-drops never duplicate)

Watch is the unattended mode of the engine of enlightenment: drop knowledge
in a folder, ask the graph questions. The idempotent graph (same sha = skip)
is what makes watch safe — a watcher re-processing the same file is a no-op.

Usage:
  python3 watch.py --dir ~/Dropbox/consumer-inbox --db ~/knowledge.graph.db
  python3 watch.py --dir ... --db ... --interval 10 --once   (one sweep, exit)

Design: stdlib only. Polling (os.scandir + mtime/sha compare) over FSEvents —
portable (works on Linux/macOS/Windows), zero deps, and the cost is one
scandir per interval. Heavy STT during watch: run this on the batch box, not
the daily driver.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[watch {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def snapshot(dirpath):
    """path -> (mtime, size) for every regular file below dirpath."""
    state = {}
    for root, dirs, files in os.walk(dirpath):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "__pycache__", ".venv", "venv",
                    "extracted", "transcripts"}]
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
                state[p] = (st.st_mtime, st.st_size)
            except OSError:
                pass
    return state


def process_file(path, db, workspace):
    """Run one file through the full chain. Each stage is a subprocess call
    to the same tools a human would run — one code path, no reimplementation."""
    stage = os.path.join(workspace, "staging")
    os.makedirs(stage, exist_ok=True)
    # copy into staging so ingest's extracted/ doesn't pollute the watched dir
    import shutil
    target = os.path.join(stage, os.path.basename(path))
    if os.path.abspath(path) != os.path.abspath(target):
        shutil.copy2(path, target)

    r = subprocess.run(["python3", os.path.join(HERE, "ingest.py"), stage, "--recursive"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        log(f"ingest failed: {os.path.basename(path)}: {(r.stderr or '')[-120:]}")
        return
    corpus_dir = os.path.join(stage, "extracted")
    cj = os.path.join(corpus_dir, "corpus.json")
    if not os.path.exists(cj):
        return
    corpus = json.load(open(cj))
    flags = {os.path.basename(e["file"]): (e.get("needs_ocr"), e.get("needs_transcription"))
             for e in corpus["files"]}

    # OCR lane when needed
    if any(v[0] for v in flags.values()):
        subprocess.run(["python3", os.path.join(HERE, "ocr.py"), stage],
                       capture_output=True, text=True, timeout=3600)
    # STT lane when needed
    for e in corpus["files"]:
        if e.get("needs_transcription"):
            venv = os.environ.get("BRADAN_VENV_PYTHON",
                                 os.path.expanduser("~/.hermes/venvs/bradan/bin/python"))
            tdir = os.path.join(stage, "transcripts")
            os.makedirs(tdir, exist_ok=True)
            subprocess.run(["python3", os.path.join(HERE, "transcribe.py"), e["file"],
                            "--out-dir", tdir, "--venv-python", venv],
                           capture_output=True, text=True, timeout=7200)

    # graph (idempotent) — extracted text
    r2 = subprocess.run(["python3", os.path.join(HERE, "graph.py"), "ingest",
                         corpus_dir, "--db", db],
                        capture_output=True, text=True, timeout=3600)
    if r2.returncode == 0:
        g = json.loads(r2.stdout)
        log(f"{os.path.basename(path)} -> graph: +{g['sources']} src, "
            f"+{g['chunks']} chunks (skipped {g['skipped']})")
    # transcripts too
    tdir = os.path.join(stage, "transcripts")
    if os.path.isdir(tdir) and os.listdir(tdir):
        r3 = subprocess.run(["python3", os.path.join(HERE, "graph.py"), "ingest",
                             tdir, "--db", db],
                            capture_output=True, text=True, timeout=3600)
        if r3.returncode == 0:
            log(f"transcripts -> graph: +{json.loads(r3.stdout)['sources']} src")


def main():
    ap = argparse.ArgumentParser(description="Drop-folder watcher: files assimilate themselves")
    ap.add_argument("--dir", required=True, help="the watched drop folder")
    ap.add_argument("--db", required=True, help="the target graph db")
    ap.add_argument("--workspace", default=None, help="staging dir (default: <db>.watch/)")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--once", action="store_true", help="one sweep then exit (cron mode)")
    a = ap.parse_args()

    watched = os.path.abspath(a.dir)
    if not os.path.isdir(watched):
        sys.exit(f"FATAL: {watched} is not a dir")
    workspace = a.workspace or (os.path.abspath(a.db) + ".watch")
    os.makedirs(watched, exist_ok=True)

    prev = {} if a.once else snapshot(watched)
    log(f"watching {watched} "
        f"({'full sweep (--once)' if a.once else f'{len(prev)} existing files skipped'}) "
        f"-> {os.path.abspath(a.db)}")
    if a.once:
        # first sweep processes everything new vs nothing (full ingest)
        pass
    while True:
        try:
            cur = snapshot(watched)
            for p, meta in cur.items():
                if prev.get(p) != meta:
                    ext = os.path.splitext(p)[1].lower()
                    if ext in (".part", ".crdownload", ".tmp", ".download"):
                        continue
                    log(f"new/changed: {os.path.basename(p)}")
                    process_file(p, a.db, workspace)
            prev = cur
            if a.once:
                break
            time.sleep(a.interval)
        except KeyboardInterrupt:
            log("bye")
            break


if __name__ == "__main__":
    main()