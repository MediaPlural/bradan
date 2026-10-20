#!/usr/bin/env python3
"""consumer course-dl — whole-course downloader: all modules, all content types.

Given a course root URL (Kajabi, HighLevel, Gumroad, Coursera, FB courses,
Teachable, Thinkific, or any platform with a normal HTML course area), crawl
same-origin course pages, download linked files (pdf/csv/xlsx/pptx/docx/zip/
mp3/mp4...), and record every acquisition in acquisition.json with sha256.

Auth: course platforms gate content behind login. This tool uses a browser
cookie jar — export cookies for the course domain from your logged-in session
(and/or pass --header "Cookie: ..." for quick use). It does NOT break DRM or
paywalls; use it on courses you own.

Media on YouTube/Vimeo/Wistia etc. is handed to yt-dlp (which also honors
cookies via --cookies/--cookies-from-browser if you need it).

Usage:
  python3 course-dl.py https://mysite.kajabi.com/courses/my-course \\
      --out-dir ./my-course --cookies cookies.txt [--max-pages 200]

Outputs: my-course/{pages/*.txt, files/*, media/*, acquisition.json, index.json}
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

FILE_EXTS = (".pdf", ".csv", ".xlsx", ".xlsm", ".docx", ".pptx", ".epub", ".zip",
             ".mp3", ".mp4", ".m4a", ".wav", ".png", ".jpg", ".jpeg", ".webp",
             ".svg", ".txt", ".md", ".json", ".tsv", ".srt", ".vtt", ".mov", ".mkv")
EMBED_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "wistia.net", "wistia.com",
               "player.vimeo", "kajabi", "highlevel", "gumroad", "cloudflarestream",
               "mux", "skool")
# skool.com: lessons are {community}/classroom/{id}?md={32-hex}; the full
# lesson list is embedded in classroom page source (no JS needed), and
# videos resolve via yt-dlp with cookies + skool referer (Cloudflare Stream).
SKOOL_MD_RE = re.compile(r"md=([a-f0-9]{32})")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15 consumer/0.1"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Fetcher:
    def __init__(self, cookie_jar=None, extra_headers=None, platform=None):
        """platform: when set, credentials come from the segmented store
        (creds.Broker) instead of a raw cookie file — the credential-broker
        pattern: the pipeline never touches credential material, the broker
        attaches it at the network boundary. Legacy --cookies still works."""
        self.broker = None
        if platform:
            import creds
            self.broker = creds.Broker(platform)
            self.cookies = ""
            self.extra = extra_headers or {}
            return
        self.cookies = ""
        if cookie_jar and os.path.exists(cookie_jar):
            # Netscape cookie jar -> Cookie header (good enough for one domain)
            pairs = []
            for line in open(cookie_jar, errors="replace"):
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 7:
                    pairs.append(f"{parts[5]}={parts[6]}")
            self.cookies = "; ".join(pairs)
        self.extra = extra_headers or {}

    def headers(self, referer=None):
        # Note: in broker mode (platform creds), get() routes through
        # broker.get() — credentials never pass through this method.
        h = {"User-Agent": UA, "Accept": "*/*"}
        if self.cookies:
            h["Cookie"] = self.cookies
        if referer:
            h["Referer"] = referer
        h.update(self.extra)
        return h

    def get(self, url, referer=None, binary=False, timeout=60):
        if self.broker is not None and self.broker.has_credentials:
            return self.broker.get(url, referer=referer, timeout=timeout, binary=binary)
        req = urllib.request.Request(url, headers=self.headers(referer))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return data if binary else data.decode("utf-8", errors="replace")


def strip_html(html):
    from html.parser import HTMLParser

    class T(HTMLParser):
        SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self._s = [], 0

        def handle_starttag(self, tag, attrs):
            if tag in self.SKIP:
                self._s += 1
            if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in self.SKIP and self._s:
                self._s -= 1

        def handle_data(self, d):
            if not self._s and d.strip():
                self.parts.append(d)

    t = T()
    t.feed(html)
    out = re.sub(r"[ \t]+", " ", "".join(t.parts))
    return re.sub(r"\n\s*\n+", "\n\n", out).strip()


LINK_RE = re.compile(r'href=["\']([^"\'#]+)["\']|src=["\']([^"\']+)["\']', re.I)
IFRAME_RE = re.compile(r'<iframe[^>]+(?:src|data-src)=["\']([^"\']+)["\']', re.I)


class Crawler:
    def __init__(self, root_url, out_dir, fetcher, max_pages, venv_python):
        self.root = root_url
        self.base = urllib.parse.urlsplit(root_url)
        self.out = out_dir
        self.fx = fetcher
        self.max_pages = max_pages
        self.venv_python = venv_python
        self.seen = set()
        self.queue = [root_url]
        self.index = {"root": root_url, "pages": [], "files": [], "media": []}
        for sub in ("pages", "files", "media"):
            os.makedirs(os.path.join(out_dir, sub), exist_ok=True)
        self.manifest = os.path.join(out_dir, "acquisition.json")
        if os.path.exists(self.manifest):
            with open(self.manifest) as f:
                self.records = json.load(f).get("sources", [])
        else:
            self.records = []

    def norm(self, url, page_url=None):
        """Resolve + keep only same-course URLs (same host, under root path)."""
        url = urllib.parse.urljoin(page_url or self.root, url)
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https"):
            return None
        if u.netloc != self.base.netloc:
            return None
        return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, u.query, ""))

    def record(self, kind, url, path, tool, note=None):
        rec = {"kind": kind, "url": url, "file": os.path.abspath(path),
               "sha256": sha256_file(path), "tool": tool, "size": os.path.getsize(path),
               "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        # broker provenance: identity only, never credential material
        broker = getattr(self.fx, "broker", None)
        if broker is not None and broker.has_credentials:
            rec["credential"] = broker.identity()
        if note:
            rec["note"] = note
        self.records.append(rec)
        with open(self.manifest, "w") as f:
            json.dump({"sources": self.records}, f, indent=2)

    def slug(self, url, kind):
        u = urllib.parse.urlsplit(url)
        base = os.path.basename(u.path) or "index"
        stem = re.sub(r"[^a-zA-Z0-9._-]", "_", base)[:120]
        return stem if stem else kind

    def save_page(self, url, html):
        path = os.path.join(self.out, "pages", self.slug(url, "page") + ".txt")
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(self.out, "pages", self.slug(url, "page") + f".{n}.txt")
        with open(path, "w") as f:
            f.write(f"SOURCE: {url}\n\n" + strip_html(html))
        self.record("page", url, path, "stdlib-html")
        self.index["pages"].append({"url": url, "file": path})

    def save_file(self, url, referer):
        data = self.fx.get(url, referer=referer, binary=True, timeout=300)
        path = os.path.join(self.out, "files", self.slug(url, "file"))
        with open(path, "wb") as f:
            f.write(data)
        self.record("file", url, path, "urllib")
        self.index["files"].append({"url": url, "file": path})

    def save_media(self, url, referer):
        ytdlp = os.path.join(os.path.dirname(self.venv_python), "yt-dlp")
        pre = [ytdlp] if os.path.exists(ytdlp) else [self.venv_python, "-m", "yt_dlp"]
        cmd = pre + ["-f", "bv*+ba/b", "--no-playlist", "--no-warnings",
                     "-o", os.path.join(self.out, "media", "%(title).80s.%(ext)s"), url]
        # skool videos sit behind Cloudflare Stream hotlink protection: the
        # referer is required or the manifest 403s
        if "skool.com" in url or "skool.com" in (referer or ""):
            cmd += ["--referer", "https://www.skool.com"]
        # broker boundary: if a broker is attached, hand yt-dlp the
        # segmented slot file — never a copy, never an inline cookie value
        broker = getattr(self.fx, "broker", None)
        if broker is not None and broker.has_credentials:
            jar = broker.cookie_file_arg()
            if jar:
                cmd += ["--cookies", jar]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        got = re.findall(r"Merging formats into \"(.+?)\"", r.stdout) or \
              re.findall(r"\[download\] Destination: (.+)", r.stdout) or \
              re.findall(r"\[download\] (.+?) has already been downloaded", r.stdout)
        if r.returncode == 0 and got:
            path = got[-1].strip()
            self.record("media", url, path, "yt-dlp")
            self.index["media"].append({"url": url, "file": path})
        else:
            self.index["media"].append({"url": url, "file": None,
                                        "error": (r.stderr or "")[-300:]})

    def crawl(self):
        pages = 0
        while self.queue and pages < self.max_pages:
            url = self.queue.pop(0)
            if url in self.seen:
                continue
            self.seen.add(url)
            pages += 1
            print(f"[course-dl] ({pages}/{self.max_pages}) {url}", file=sys.stderr)
            try:
                html = self.fx.get(url, referer=self.root)
            except Exception as e:
                print(f"  fetch failed: {e}", file=sys.stderr)
                continue
            self.save_page(url, html)

            # skool classrooms: the lesson list (md= hashes) is in page source
            if "skool.com" in url and "classroom" in url:
                for md in SKOOL_MD_RE.findall(html):
                    base = url.split("?")[0]
                    lesson = f"{base}?md={md}"
                    if lesson not in self.seen and lesson not in self.queue:
                        self.queue.append(lesson)

            for href, src in LINK_RE.findall(html):
                link = self.norm(href or src, url)
                if not link:
                    continue
                ext = os.path.splitext(urllib.parse.urlsplit(link).path)[1].lower()
                if ext in FILE_EXTS:
                    if link not in self.seen:
                        self.seen.add(link)
                        try:
                            self.save_file(link, url)
                        except Exception as e:
                            print(f"  file failed {link}: {e}", file=sys.stderr)
                elif ext in (".html", ".htm", "") or "course" in link or "module" in link or "lesson" in link:
                    if link not in self.seen:
                        self.queue.append(link)

            for embed in IFRAME_RE.findall(html):
                if any(h in embed for h in EMBED_HOSTS):
                    self.save_media(embed, url)

        with open(os.path.join(self.out, "index.json"), "w") as f:
            json.dump(self.index, f, indent=2)
        print(f"[course-dl] done: {len(self.index['pages'])} pages, "
              f"{len(self.index['files'])} files, {len(self.index['media'])} media "
              f"(media errors: {sum(1 for m in self.index['media'] if m.get('error'))})",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Whole-course downloader (all modules, all content types)")
    ap.add_argument("url", help="course root URL (logged-in session required for gated platforms)")
    ap.add_argument("--out-dir", default="./course-dl")
    ap.add_argument("--cookies", default=None, help="Netscape cookie jar export for the course domain (legacy)")
    ap.add_argument("--platform", default=None,
                    help="use segmented credentials from the store (creds.py init/import) — e.g. --platform skool")
    ap.add_argument("--header", action="append", default=[], help="extra header, e.g. --header 'Cookie: k=1' (repeatable)")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--venv-python", default=os.environ.get(
        "BRADAN_VENV_PYTHON", os.path.expanduser("~/.hermes/venvs/bradan/bin/python")))
    a = ap.parse_args()

    headers = {}
    for h in a.header:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()
    fx = Fetcher(a.cookies, headers, platform=a.platform)
    os.makedirs(a.out_dir, exist_ok=True)
    Crawler(a.url, a.out_dir, fx, a.max_pages, a.venv_python).crawl()
    print(json.dumps({"ok": True, "out_dir": os.path.abspath(a.out_dir),
                      "manifest": os.path.abspath(os.path.join(a.out_dir, "acquisition.json"))}, indent=2))


if __name__ == "__main__":
    main()