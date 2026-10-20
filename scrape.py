#!/usr/bin/env python3
"""consumer scrape — acquire course material: video links (yt-dlp) or web pages (stdlib).

Writes an acquisition manifest (source, sha256, timestamp, tool) next to the
media so every downstream transcript is provably anchored to its origin.

Usage:
  python scrape.py URL [--out-dir DIR] [--audio-only] [--venv-python PATH]
  - video platforms -> yt-dlp (pip-installed into the consumer venv)
  - anything else   -> fetched with urllib, tags stripped, saved as .txt
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from html.parser import HTMLParser

VIDEO_HINTS = ("youtube.com", "youtu.be", "vimeo.com", "udemy.com", "teachable",
               "kajabi", "thinkific", "skillshare", "coursera.org", "bilibili.com",
               "twitch.tv", "x.com", "twitter.com", "facebook.com", "instagram.com",
               "tiktok.com", "linkedin.com")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TextExtract(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data)

    def text(self):
        out = "".join(self.parts)
        out = re.sub(r"[ \t]+", " ", out)
        out = re.sub(r"\n\s*\n+", "\n\n", out)
        return out.strip()


def fetch_page(url, out_dir):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (consumer/0.1)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", errors="replace")
    p = TextExtract()
    p.feed(raw)
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    stem = re.sub(r"[^a-z0-9]+", "-", (title.group(1) if title else url).strip().lower())[:60].strip("-") or "page"
    path = os.path.join(out_dir, stem + ".txt")
    with open(path, "w") as f:
        f.write(p.text())
    return path


def fetch_video(url, out_dir, venv_python, audio_only):
    ytdlp = os.path.join(os.path.dirname(venv_python), "yt-dlp")
    if not os.path.exists(ytdlp):
        ytdlp = venv_python
        pre = [venv_python, "-m", "yt_dlp"]
    else:
        pre = [ytdlp]
    fmt = "bestaudio/best" if audio_only else "bv*+ba/b"
    ext = "m4a" if audio_only else "mp4"
    cmd = pre + ["-f", fmt, "--no-playlist", "--no-warnings",
                 "-o", os.path.join(out_dir, "%(title).80s.%(ext)s"), url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        sys.exit(f"FATAL: yt-dlp failed:\n{(r.stderr or '')[-800:]}")
    got = re.findall(r"\[download\] Destination: (.+)", r.stdout) or \
          re.findall(r"Merging formats into \"(.+?)\"", r.stdout) or \
          re.findall(r"\[download\] (.+?) has already been downloaded", r.stdout)
    return got[-1].strip() if got else None


def manifest(out_dir, url, path, tool):
    mpath = os.path.join(out_dir, "acquisition.json")
    m = {"sources": []}
    if os.path.exists(mpath):
        with open(mpath) as f:
            m = json.load(f)
    m["sources"].append({
        "url": url, "file": os.path.abspath(path), "sha256": sha256_file(path),
        "tool": tool, "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    with open(mpath, "w") as f:
        json.dump(m, f, indent=2)
    return mpath


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out-dir", default="./acquired")
    ap.add_argument("--audio-only", action="store_true", help="video -> audio (smaller, enough for transcript)")
    ap.add_argument("--venv-python", default=os.environ.get(
        "BRADAN_VENV_PYTHON", os.path.expanduser("~/.hermes/venvs/bradan/bin/python")))
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)

    is_video = any(h in a.url for h in VIDEO_HINTS)
    if is_video:
        path = fetch_video(a.url, a.out_dir, a.venv_python, a.audio_only)
        if not path or not os.path.exists(path):
            sys.exit("FATAL: could not locate downloaded file; run yt-dlp manually to debug")
        tool = "yt-dlp"
    else:
        path = fetch_page(a.url, a.out_dir)
        tool = "urllib+stdlib-html"

    mpath = manifest(a.out_dir, a.url, path, tool)
    print(json.dumps({"ok": True, "file": path, "sha256": sha256_file(path), "manifest": mpath}, indent=2))


if __name__ == "__main__":
    main()