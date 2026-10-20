#!/usr/bin/env python3
"""consumer source — point the consumer at ANYTHING and get a normalized ingest dir.

The engine of enlightenment starts here: a source is whatever you point at —
a website, a zip, a Google Drive link, a folder, a single file, a course URL,
a plain URL. source.py resolves it, acquires/copies it into a workspace, and
returns a directory the rest of the pipeline (ingest/transcribe/distill/graph)
consumes uniformly.

Usage (usually via consumer.py, which calls resolve_source()):
  python3 source.py https://example.com/docs --workspace ./ws
  python3 source.py ~/Downloads/course-export.zip --workspace ./ws
  python3 source.py "https://drive.google.com/file/d/ID/view" --workspace ./ws
  python3 source.py ~/my-course --workspace ./ws

Resolution order:
  local dir          -> copied (or referenced) as-is
  local file (.zip)  -> extracted
  local file (other) -> copied (ingest.py will extract per-format)
  http(s) zip        -> downloaded + extracted
  http(s) HTML       -> fetched (single page via scrape.py semantics)
  course platform    -> full crawl via course-dl.py (skool/Kajabi/HighLevel/
                        Gumroad/Coursera/Teachable/Thinkific)
  Google Drive       -> public file/folder via the uc/download endpoint
                        (private folders need your own auth — documented, not bypassed)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

COURSE_HINTS = ("skool.com", "kajabi", "gohighlevel", "gumroad", "coursera",
                "teachable", "thinkific", "udemy")
GDRIVE_FILE = re.compile(r"drive\.google\.com/file/d/([^/]+)")
GDRIVE_OPEN = re.compile(r"drive\.google\.com/open\?id=([^&]+)")
GDRIVE_UC = re.compile(r"drive\.google\.com/uc\?id=([^&]+)")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15 consumer/0.1"


def is_url(s):
    return s.startswith("http://") or s.startswith("https://")


def is_zip_head(path_or_bytes):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return bytes(path_or_bytes[:2]) == b"PK"
    with open(path_or_bytes, "rb") as f:
        return f.read(2) == b"PK"


def extract_zip(zip_path, dest):
    """Safe extraction: no path escapes, no absolute paths, no symlinks."""
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if name.startswith(("/", "\\")) or ".." in name.split("/"):
                continue
            if info.is_dir() or name.endswith("/"):
                continue
            target = os.path.join(dest, name)
            if not os.path.abspath(target).startswith(os.path.abspath(dest) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return dest


def download(url, dest_path, timeout=600):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest_path, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest_path


def gdrive_file_id(url):
    for pat in (GDRIVE_FILE, GDRIVE_OPEN, GDRIVE_UC):
        m = pat.search(url)
        if m:
            return m.group(1)
    q = urllib.parse.urlsplit(url).query
    for part in q.split("&"):
        if part.startswith("id="):
            return part[3:]
    return None


def fetch_gdrive_file(fid, dest):
    """Public Drive files download via the uc endpoint with confirm-token
    handling. Private files need your own credentials — not bypassed."""
    base = f"https://drive.google.com/uc?export=download&id={fid}"
    # small files: direct
    try:
        download(base, dest, timeout=120)
        with open(dest, "rb") as f:
            if f.read(2) == b"PK" or not f.read(400).lower().startswith(b"<!doctype html"):
                return dest
    except Exception:
        pass
    # large files: confirm token page
    try:
        req = urllib.request.Request(base, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            page = r.read().decode("utf-8", errors="replace")
        m = re.search(r"confirm=([A-Za-z0-9_-]+)", page) or re.search(r'name="uuid" value="([^"]+)"', page)
        if m:
            dl = base + f"&confirm={m.group(1)}"
            download(dl, dest)
            return dest
    except Exception as e:
        sys.exit(f"FATAL: gdrive download failed for {fid}: {e}")
    sys.exit(f"FATAL: could not fetch gdrive file {fid} (private? use a public link "
             f"or export via your own Drive auth)")


def fetch_gdrive_folder(url, dest):
    """Public Drive folders: the folder view HTML carries file entries; the
    reliable stdlib path is the embedded-items JSON. Folders with many files
    or private ACLs: export a zip from Drive UI and point consumer at that."""
    sys.exit("FATAL: gdrive FOLDER crawl needs embedded-UI auth in the general case — "
             "use Drive's 'Download as ZIP' and point consumer at the zip, or make "
             "individual files public and pass their links (one per run). "
             "This is a documented limit, not a wall we route around.")


def run_course_dl(url, workspace):
    out = os.path.join(workspace, "course")
    cmd = ["python3", os.path.join(HERE, "course-dl.py"), url, "--out-dir", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        sys.exit(f"FATAL: course-dl failed:\n{(r.stderr or '')[-600:]}")
    return out


def run_scrape(url, workspace):
    out = os.path.join(workspace, "acquired")
    cmd = ["python3", os.path.join(HERE, "scrape.py"), url, "--out-dir", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        sys.exit(f"FATAL: scrape failed:\n{(r.stderr or '')[-600:]}")
    return out


def resolve_source(source, workspace):
    """Return (kind, ingest_dir). ingest_dir holds the raw material the
    pipeline consumes: files to ingest, media to transcribe, pages to distill."""
    os.makedirs(workspace, exist_ok=True)
    incoming = os.path.join(workspace, "incoming")
    os.makedirs(incoming, exist_ok=True)

    # local dir
    if os.path.isdir(source):
        dest = os.path.join(incoming, os.path.basename(os.path.abspath(source)) or "dir")
        if os.path.abspath(source) != os.path.abspath(dest):
            shutil.copytree(source, dest, dirs_exist_ok=True,
                           ignore=shutil.ignore_patterns("__pycache__", ".git"))
        return "dir", dest

    # local file
    if os.path.isfile(source):
        if zipfile.is_zipfile(source):
            dest = os.path.join(incoming, os.path.splitext(os.path.basename(source))[0])
            return "zip", extract_zip(source, dest)
        dest = os.path.join(incoming, os.path.basename(source))
        shutil.copy2(source, dest)
        return "file", incoming

    if not is_url(source):
        sys.exit(f"FATAL: source not found locally and not a URL: {source}")

    # gdrive
    if "drive.google.com" in source:
        if "/folders/" in source:
            return "gdrive-folder", fetch_gdrive_folder(source, incoming)
        fid = gdrive_file_id(source)
        if not fid:
            sys.exit("FATAL: could not parse gdrive file id from the URL")
        ext = ".zip"
        dest = os.path.join(incoming, f"gdrive-{fid}{ext}")
        fetch_gdrive_file(fid, dest)
        if zipfile.is_zipfile(dest):
            return "gdrive-zip", extract_zip(dest, os.path.join(incoming, f"gdrive-{fid}"))
        return "gdrive-file", incoming

    # course platforms -> full crawl
    if any(h in source for h in COURSE_HINTS):
        return "course", run_course_dl(source, workspace)

    # remote zip?
    u = urllib.parse.urlsplit(source)
    if u.path.lower().endswith(".zip"):
        dest = os.path.join(incoming, os.path.basename(u.path) or "download.zip")
        download(source, dest)
        return "zip-url", extract_zip(dest, os.path.join(incoming, "extracted"))

    # plain web page
    return "url", run_scrape(source, workspace)


def main():
    ap = argparse.ArgumentParser(description="Point the consumer at ANY source; get a normalized ingest dir")
    ap.add_argument("source")
    ap.add_argument("--workspace", default="./consumer-workspace")
    ap.add_argument("--full", action="store_true",
                    help="chain ingest + graph automatically (the engine path)")
    ap.add_argument("--db", default=os.environ.get("BRADAN_GRAPH_DB", "consumer.graph.db"))
    a = ap.parse_args()
    kind, ingest_dir = resolve_source(a.source, a.workspace)
    out = {"ok": True, "kind": kind, "ingest_dir": os.path.abspath(ingest_dir),
           "workspace": os.path.abspath(a.workspace)}
    if a.full:
        # extract text from every file format -> extracted/ dir with corpus.json
        r = subprocess.run(["python3", os.path.join(HERE, "ingest.py"),
                            ingest_dir, "--recursive"],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            sys.exit(f"FATAL: ingest failed:\n{(r.stderr or '')[-500:]}")
        ing = json.loads(r.stdout)

        # MEDIA GAP (the one-command path must close it): videos/audio never
        # reach the graph unless we transcribe them here. corpus.json flags
        # them needs_transcription; the STT lane turns them into transcript
        # text, which the graph then ingests as first-class sources.
        # NOTE: ingest's stdout has files=N (count) and files_list=[...] —
        # iterate the list, not the int (TypeError bug caught by the suite).
        media = [e for e in ing.get("files_list", []) if e.get("needs_transcription")]
        transcript_dir = None
        if media:
            venv = os.environ.get("BRADAN_VENV_PYTHON",
                                  os.path.expanduser("~/.hermes/venvs/bradan/bin/python"))
            transcript_dir = os.path.join(os.path.dirname(ingest_dir), "transcripts")
            os.makedirs(transcript_dir, exist_ok=True)
            done = 0
            for e in media:
                tr = subprocess.run(["python3", os.path.join(HERE, "transcribe.py"),
                                     e["file"], "--out-dir", transcript_dir,
                                     "--venv-python", venv],
                                    capture_output=True, text=True, timeout=7200)
                done += 1 if tr.returncode == 0 else 0
            ing["media_transcribed"] = done
            ing["media_total"] = len(media)

        corpus_dir = os.path.join(ingest_dir, "extracted") if os.path.isdir(
            os.path.join(ingest_dir, "extracted")) else ingest_dir
        # vectorized graph (transcripts land beside corpus.json: bare-dir path)
        graph_cmd = ["python3", os.path.join(HERE, "graph.py"), "ingest", corpus_dir, "--db", a.db]
        r2 = subprocess.run(graph_cmd, capture_output=True, text=True, timeout=3600)
        if r2.returncode != 0:
            sys.exit(f"FATAL: graph ingest failed:\n{(r2.stderr or '')[-500:]}")
        out["graph"] = json.loads(r2.stdout)
        # transcripts are their own source class in the graph
        if transcript_dir and os.listdir(transcript_dir):
            r3 = subprocess.run(["python3", os.path.join(HERE, "graph.py"), "ingest",
                                 transcript_dir, "--db", a.db],
                                capture_output=True, text=True, timeout=3600)
            if r3.returncode == 0:
                out["graph"]["transcript_sources"] = json.loads(r3.stdout).get("sources")
        out["next"] = (f"python3 graph.py search/semantic/hybrid/filter/insight/maths --db {a.db}")
    else:
        out["next"] = f"python3 ingest.py {ingest_dir} --recursive"
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()