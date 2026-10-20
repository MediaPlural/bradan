#!/usr/bin/env python3
"""consumer creds — segmented credential store + network-boundary injection.

Pattern credit: a privilege-separated security architecture (auth daemon /
privsep / permission authority), simplified to a stdlib-only repo. The
load-bearing split:

  THE PIPELINE NEVER TOUCHES CREDENTIAL FILES.
  creds.broker() is the ONLY code that reads them — credentials attach at the
  network boundary, inside this module, and raw cookies/tokens never flow
  back into course-dl/scrape/yt-dlp command lines or logs.

Segmentation (per-platform isolation):
  ~/.consumer/creds/<platform>/cookies.txt   Netscape jar, 0600, per platform
  ~/.consumer/creds/<platform>/headers.json   extra headers, 0600, per platform
  A leak of one platform's jar cannot cascade to another; the store root is
  0700; nothing is ever copied out of it.

What the pipeline sees:
  - creds.broker(platform) -> Broker object with .get(url) / .cookie_args()
  - acquisition manifests record identity only: {"credential": "platform:skool"}
  - error messages redact values; --verify prints file presence + expiry, never contents

CLI:
  python3 creds.py init skool            # create the segmented slot (0600)
  python3 creds.py import skool file.txt # place a Netscape cookie jar into the slot
  python3 creds.py verify skool          # presence + mtime + cookie count (no values)
  python3 creds.py list                  # which platforms have slots
"""
import argparse
import http.cookiejar
import json
import os
import stat
import sys
import time
import urllib.request

CREDS_ROOT = os.environ.get("BRADAN_CREDS_ROOT",
                            os.path.expanduser("~/.consumer/creds"))


def _parse_jar_tolerant(path):
    """Manual Netscape-format parse. The stdlib MozillaCookieJar asserts
    domain-must-start-with-dot when include_subdomains=TRUE — real browser
    exports violate that constantly, so the broker parses tolerantly and
    keeps cookies in a simple list of (domain, name, value, expires)."""
    cookies = []
    for line in open(path, errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("#HttpOnly_"):
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            else:
                continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, _secure, expires, name, value = parts[:7]
        try:
            expires = int(float(expires))
        except ValueError:
            expires = 0
        cookies.append({"domain": domain.lower().lstrip("."), "path": path,
                        "expires": expires, "name": name, "value": value})
    return cookies


class Broker:
    """One platform's credentials, usable only through this object.
    get() attaches the Cookie header inside the boundary; the caller receives
    bytes/text — never the credential material."""

    def __init__(self, platform):
        self.platform = platform
        self.dir = os.path.join(CREDS_ROOT, platform)
        self.jar_path = os.path.join(self.dir, "cookies.txt")
        self.headers_path = os.path.join(self.dir, "headers.json")
        self._jar = None          # stdlib jar (well-formed exports)
        self._cookies = []        # tolerant parse (real-world exports)
        self._extra = {}
        if os.path.exists(self.headers_path):
            with open(self.headers_path) as f:
                self._extra = json.load(f)
        if os.path.exists(self.jar_path):
            try:
                jar = http.cookiejar.MozillaCookieJar(self.jar_path)
                jar.load(ignore_discard=True, ignore_expires=True)
                self._jar = jar
            except Exception:
                # stdlib assertion bugs out on many real exports — parse manually
                self._cookies = _parse_jar_tolerant(self.jar_path)

    @property
    def has_credentials(self):
        if self._extra:
            return True
        if self._jar is not None and len(self._jar) > 0:
            return True
        return len(self._cookies) > 0

    def _cookie_pairs(self, url):
        """All cookie name=value pairs that apply to this URL — from the
        stdlib jar when it loaded, else the tolerant list. Boundary: pairs
        are consumed here; nothing escapes this module."""
        pairs = []
        host = urllib.parse.urlsplit(url).netloc.lower()
        now = time.time()
        if self._jar is not None:
            req = urllib.request.Request(url)
            for c in self._jar:
                if c.expires is not None and c.expires < now:
                    continue
                if self._jar._policy.return_ok(c, req):
                    pairs.append(f"{c.name}={c.value}")
        for c in self._cookies:
            if c["expires"] and c["expires"] < now:
                continue
            if c["domain"] and c["domain"] not in host and not host.endswith("." + c["domain"]):
                continue
            pairs.append(f"{c['name']}={c['value']}")
        return pairs

    def cookie_header(self, url):
        """Cookies for this URL, assembled inside the boundary."""
        pairs = self._cookie_pairs(url)
        return "; ".join(pairs) if pairs else None

    def headers_for(self, url, referer=None):
        h = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                           "Safari/605.1.15 consumer/0.1"}
        ch = self.cookie_header(url)
        if ch:
            h["Cookie"] = ch
        if referer:
            h["Referer"] = referer
        for k, v in self._extra.items():
            h[k] = v
        return h

    def get(self, url, referer=None, timeout=60, binary=False):
        """Authenticated fetch. Credentials attach HERE (boundary), are
        stripped from any exception text, and never return to the caller."""
        req = urllib.request.Request(url, headers=self.headers_for(url, referer))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # redact: never let query strings carrying tokens leak into errors
            raise urllib.error.HTTPError(
                url.split("?")[0], e.code, "redacted", e.headers, None) from None

    def cookie_file_arg(self):
        """For tools that must have a file (yt-dlp). Returns the slot path —
        still segmented per platform, never copied, never echoed to logs.
        Returns None when no jar exists."""
        return self.jar_path if os.path.exists(self.jar_path) else None

    def identity(self):
        return f"platform:{self.platform}"


def _ensure_root():
    os.makedirs(CREDS_ROOT, mode=0o700, exist_ok=True)
    os.chmod(CREDS_ROOT, 0o700)


def cmd_init(platform):
    _ensure_root()
    d = os.path.join(CREDS_ROOT, platform)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    print(json.dumps({"ok": True, "slot": d, "mode": "0700",
                      "next": f"place cookies at {d}/cookies.txt "
                              f"(or: creds.py import {platform} cookies.txt)"}))


def cmd_import(platform, src):
    _ensure_root()
    if not os.path.exists(src):
        sys.exit(f"FATAL: no such file: {src}")
    d = os.path.join(CREDS_ROOT, platform)
    os.makedirs(d, mode=0o700, exist_ok=True)
    dst = os.path.join(d, "cookies.txt")
    with open(src, "rb") as fi, open(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fo:
        fo.write(fi.read())
    os.chmod(dst, 0o600)
    # remove the source jar from wherever it was exported — segmentation means
    # ONE copy, in the store (best effort; read-only sources left alone)
    try:
        if os.path.dirname(os.path.abspath(src)) != d:
            os.remove(src)
            removed = True
        else:
            removed = False
    except OSError:
        removed = False
    b = Broker(platform)
    print(json.dumps({"ok": True, "slot": dst, "mode": "0600",
                      "cookies": len(b._jar) if b._jar else 0,
                      "source_removed": removed}, indent=2))


def cmd_verify(platform):
    b = Broker(platform)
    exists = os.path.exists(b.jar_path)
    if b._jar is not None:
        n = len(b._jar)
        fresh = sum(1 for c in b._jar if c.expires is None or c.expires > time.time())
    else:
        n = len(b._cookies)
        fresh = sum(1 for c in b._cookies if not c["expires"] or c["expires"] > time.time())
    exp = os.path.getmtime(b.jar_path) if exists else None
    print(json.dumps({"platform": platform,
                      "cookies_file": exists,
                      "cookies_total": n,
                      "cookies_fresh": fresh,
                      "jar_modified": exp and time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)),
                      "extra_headers": list(b._extra.keys()),
                      "identity": b.identity(),
                      "note": "values are never printed"}, indent=2))


def cmd_list():
    _ensure_root()
    rows = []
    for platform in sorted(os.listdir(CREDS_ROOT)):
        b = Broker(platform)
        if os.path.isdir(b.dir):
            rows.append({"platform": platform,
                         "has_credentials": b.has_credentials})
    print(json.dumps({"root": CREDS_ROOT, "slots": rows}, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Segmented credential store (credential-broker pattern)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("init"); p1.add_argument("platform")
    p2 = sub.add_parser("import"); p2.add_argument("platform"); p2.add_argument("cookie_file")
    p3 = sub.add_parser("verify"); p3.add_argument("platform")
    p4 = sub.add_parser("list")
    a = ap.parse_args()
    {"init": lambda: cmd_init(a.platform),
     "import": lambda: cmd_import(a.platform, a.cookie_file),
     "verify": lambda: cmd_verify(a.platform),
     "list": cmd_list}[a.cmd]()


if __name__ == "__main__":
    main()