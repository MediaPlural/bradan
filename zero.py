#!/usr/bin/env python3
"""consumer zero — zero-in: consume exactly what you're looking at.

The Foxy-select/Omnisight trigger, consumer edition: an agent (or hotkey,
or you) fires zero.py and whatever is on screen — a region you drag, a
window, the full screen — flows through the engine:

  capture (screencapture) -> OCR (Vision, local) -> graph (attributed)

Zero-in is the shortest path in the whole engine: "consume THIS" — a Slack
thread, a chart, an error message, a slide, a tweet — one trigger, and it
becomes queryable knowledge with a screenshot as the provenance artifact.

Usage:
  python3 zero.py --region          # interactive: drag a box (Foxy-select style)
  python3 zero.py --window          # click a window to consume it
  python3 zero.py --fullscreen      # everything on screen
  python3 zero.py --clip            # consume the clipboard text directly
  all: --db GRAPH.db [--keep DIR] [--label "what this is"]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def ocr_image(path):
    import ocr
    helper = ocr.ensure_helper()
    if not helper:
        sys.exit("FATAL: OCR engine unavailable (macOS: swiftc; Linux: tesseract)")
    return ocr.ocr_file(path, helper)


def capture(kind, keep_dir):
    ts = time.strftime("%Y%m%d-%H%M%S")
    if keep_dir:
        os.makedirs(keep_dir, exist_ok=True)
        out = os.path.join(keep_dir, f"zero-{ts}.png")
    else:
        out = os.path.join(os.path.expanduser("~/.consumer/zero"), f"zero-{ts}.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)

    if kind == "clip":
        return None, out  # clipboard handled separately
    flags = {"region": ["-i", "-s"], "window": ["-i", "-w"], "fullscreen": ["-x"]}
    cmd = ["screencapture"] + flags[kind] + [out]
    print(f"[zero] {kind}: capture now…" if kind != "fullscreen" else "[zero] capturing…",
          file=sys.stderr, flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return (os.path.exists(out), out)


def clip_text():
    r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else ""


def ingest_text(text, label, db):
    """Write to a staging dir -> ingest -> graph (attribution: zero-in)."""
    stage = os.path.join(os.path.expanduser("~/.consumer/zero/staging"),
                         time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(stage, exist_ok=True)
    fname = (label or "zero-in") + ".txt"
    with open(os.path.join(stage, fname), "w") as f:
        f.write(text)
    # acquisition manifest = attribution: this knowledge came from zero-in
    with open(os.path.join(stage, "acquisition.json"), "w") as f:
        json.dump({"sources": [{"kind": "screen", "url": None, "file": os.path.join(stage, fname),
                                "sha256": "0" * 64, "tool": "zero-in:" + (label or "screen"),
                                "size": len(text), "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}]}, f)
    r = subprocess.run(["python3", os.path.join(HERE, "ingest.py"), stage, "--recursive"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        sys.exit(f"FATAL: ingest failed:\n{(r.stderr or '')[-300:]}")
    r2 = subprocess.run(["python3", os.path.join(HERE, "graph.py"), "ingest",
                         os.path.join(stage, "extracted"), "--db", db],
                        capture_output=True, text=True, timeout=600)
    if r2.returncode != 0:
        sys.exit(f"FATAL: graph failed:\n{(r2.stderr or '')[-300:]}")
    return json.loads(r2.stdout)


def main():
    ap = argparse.ArgumentParser(description="Zero-in: consume exactly what you're looking at")
    ap.add_argument("kind", nargs="?", default="region",
                    choices=["region", "window", "fullscreen", "clip"])
    ap.add_argument("--db", default=os.environ.get("BRADAN_GRAPH_DB", "consumer.graph.db"))
    ap.add_argument("--keep", default=None, help="dir to keep the screenshot (default ~/.consumer/zero/)")
    ap.add_argument("--label", default=None, help="what this capture is (becomes the source name)")
    a = ap.parse_args()

    if a.kind == "clip":
        text = clip_text()
        if not text.strip():
            sys.exit("FATAL: clipboard is empty")
        g = ingest_text(text, a.label or "clipboard", a.db)
        print(json.dumps({"ok": True, "kind": "clip", "chars": len(text), "graph": g}, indent=2))
        return

    ok, shot = capture(a.kind, a.keep)
    if not ok:
        sys.exit("FATAL: capture cancelled or failed")
    text = ocr_image(shot)
    if not text or not text.strip():
        print(json.dumps({"ok": True, "kind": a.kind, "chars": 0,
                          "note": "no text detected on screen region; screenshot kept",
                          "screenshot": shot}, indent=2))
        return
    g = ingest_text(text, a.label or os.path.basename(shot), a.db)
    print(json.dumps({"ok": True, "kind": a.kind, "chars": len(text),
                      "screenshot": shot, "graph": g}, indent=2))


if __name__ == "__main__":
    main()