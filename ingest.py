#!/usr/bin/env python3
"""bradan ingest — universal local-file text extractor for course content.

Input: any file (or a directory — recursive). Output: normalized .txt next to
the source (in --out-dir), plus corpus.json entries with provenance.

Formats (stdlib-only core — no API keys, no cloud):
  .txt .md .csv .json .html .htm .srt .vtt .tsv .log    direct parse
  .xlsx .xlsm .docx .pptx .epub .odt                    ZIP+XML extraction
  .svg                                                  XML text extraction
  .pdf                                                  stdlib zlib-stream text
                                                        extractor (best-effort;
                                                        scanned PDFs need OCR)
  images (.png .jpg .webp .gif .tiff .bmp .heic)       no stdlib OCR — recorded
                                                        in corpus with
                                                        needs_ocr flag (vision
                                                        adapter handles them)
  video/audio                                            routed to transcribe.py
                                                        by the wrapper; here,
                                                        recorded with
                                                        needs_transcription flag
  everything else                                       byte-registered,
                                                        extension noted

Usage:
  python3 ingest.py PATH [--out-dir DIR] [--recursive]
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

# Security note (xml_unsafe_parse warning): stdlib ET never resolves external
# entities (XXE not applicable), but internal-entity expansion (billion-laughs
# DoS) is a real vector for untrusted EPUB/SVG/OOXML content. Guard: refuse to
# parse any XML carrying DTD entity declarations — OOXML (docx/pptx/xlsx/odt)
# never contains them, so legit files are unaffected. stdlib-only by design.


def safe_xml(blob):
    """Parse XML only when free of DTD/ENTITY declarations (DoS guard).
    Accepts bytes or str — encodes to bytes for the uniform check."""
    if isinstance(blob, str):
        blob = blob.encode("utf-8", errors="replace")
    if b"<!ENTITY" in blob[:65536] or b"<!DOCTYPE" in blob[:65536]:
        raise ValueError("refusing XML with entity/DOCTYPE declarations (billion-laughs guard)")
    return ET.fromstring(blob)


def sniff(path):
    """Magic-byte sniff — course downloads are full of mislabeled files
    (a '.docx' that is really UTF-8 text; a '.pptx' that is really a PDF).
    Returns one of: pdf, zip, text, binary."""
    with open(path, "rb") as f:
        head = f.read(512)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        return "zip"
    if head.startswith(b"\xef\xbb\xbf") or b"\x00" not in head[0:512]:
        return "text"
    return "binary"

MEDIA_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4a", ".mp3", ".wav",
             ".aac", ".flac", ".aiff", ".ogg", ".m4v")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".tif", ".bmp", ".heic")
# Purchased-book formats. EPUB extracts natively (read_epub). DRM'd Kindle
# exports (.azw/.azw3/.mobi/.kfx) are byte-registered with conversion
# guidance: this tool never strips DRM; if you have a DRM-free export or
# an EPUB (e.g. via your own library tools), it ingests directly.
EBOOK_HINT = {".azw": "Kindle legacy — re-export DRM-free (e.g. EPUB) to ingest text",
               ".azw3": "Kindle KF8 — re-export DRM-free (e.g. EPUB) to ingest text",
               ".mobi": "Mobipocket — re-export DRM-free (e.g. EPUB) to ingest text",
               ".kfx": "Kindle KFX — re-export DRM-free (e.g. EPUB) to ingest text",
               ".prc": "Mobipocket legacy — re-export DRM-free (e.g. EPUB) to ingest text"}
SKIP_DIRNAMES = {".git", "node_modules", "__pycache__", ".venv", "venv", "transcripts", "distilled"}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- direct text

def read_plain(path):
    return open(path, errors="replace").read()


def read_csv_like(path, delim):
    out = []
    with open(path, newline="", errors="replace") as f:
        for row in csv.reader(f, delimiter=delim):
            out.append(delim.join(row))
    return "\n".join(out)


def read_json(path):
    return json.dumps(json.load(open(path, errors="replace")), indent=2, ensure_ascii=False)


class _TextHTML(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

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
        t = re.sub(r"[ \t]+", " ", "".join(self.parts))
        return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def read_html(path):
    p = _TextHTML()
    p.feed(open(path, errors="replace").read())
    return p.text()


def read_srt_vtt(path):
    out = []
    for line in open(path, errors="replace"):
        line = line.strip()
        if not line or "-->" in line or re.fullmatch(r"\d+", line) or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- ZIP+XML

def _xml_text(blob):
    """All visible text in an XML blob, space-separated per element."""
    try:
        root = safe_xml(blob)
    except ET.ParseError:
        return ""
    parts = []

    def walk(node):
        if node.text and node.text.strip():
            parts.append(node.text.strip())
        for child in node:
            walk(child)
        if node.tail and node.tail.strip():
            parts.append(node.tail.strip())

    walk(root)
    return "\n".join(parts)


def read_xlsx(path):
    """Sheet extraction from xlsx via zipfile — shared strings + inline values."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        blob = z.read("xl/sharedStrings.xml")
        root = safe_xml(blob)
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        for si in root.findall(f"{ns}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
    out = []
    for name in z.namelist():
        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
            out.append(f"## {name}")
            root = safe_xml(z.read(name))
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            for row in root.iter(f"{ns}row"):
                cells = []
                for c in row.iter(f"{ns}c"):
                    t = c.get("t")
                    v = c.find(f"{ns}v")
                    if t == "s" and v is not None:
                        cells.append(shared[int(v.text)])
                    elif t == "inlineStr":
                        cells.append("".join(x.text or "" for x in c.iter(f"{ns}t")))
                    elif v is not None:
                        cells.append(v.text or "")
                if cells:
                    out.append(" | ".join(cells))
    # Mac-Excel "schedule" style: text lives in drawing shapes, not cells.
    # Any drawing anchored on a sheet is part of that sheet's content.
    for name in z.namelist():
        if re.fullmatch(r"xl/drawings/drawing\d+\.xml", name):
            root = safe_xml(z.read(name))
            texts = [t.text or "" for t in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")
                     if (t.text or "").strip()]
            if texts:
                out.append(f"## {name} (drawing content)")
                out.extend(texts)
    return "\n".join(out) if out else "(no sheets parsed)"


def read_docx(path):
    z = zipfile.ZipFile(path)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = safe_xml(z.read("word/document.xml"))
    parts = []
    for para in root.iter(f"{ns}p"):
        line = "".join(t.text or "" for t in para.iter(f"{ns}t"))
        if line.strip():
            parts.append(line)
    return "\n".join(parts)


def read_pptx(path):
    z = zipfile.ZipFile(path)
    a_ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.search(r"(\d+)", n).group(1)))
    out = []
    for i, name in enumerate(slides, 1):
        root = safe_xml(z.read(name))
        texts = [t.text or "" for t in root.iter(f"{a_ns}t") if (t.text or "").strip()]
        if texts:
            out.append(f"## Slide {i}\n" + "\n".join(texts))
    return "\n".join(out) if out else "(no slide text)"


def read_epub(path):
    z = zipfile.ZipFile(path)
    docs = sorted(n for n in z.namelist() if n.endswith((".xhtml", ".html", ".htm")))
    out = []
    for name in docs:
        p = _TextHTML()
        p.feed(z.read(name).decode("utf-8", errors="replace"))
        t = p.text()
        if t:
            out.append(t)
    return "\n\n".join(out)


def read_svg(path):
    return _xml_text(open(path, errors="replace").read())


# ---------------------------------------------------------------- PDF (best-effort)

def read_pdf(path):
    """Best-effort stdlib PDF text: decompress content streams, pull text-
    showing operators. Handles most text PDFs; scanned PDFs need OCR.

    Perf note: no lazy/alternation regexes over the whole stream (the first
    version used `stream\\r?\\n(.*?)endstream` + bracket alternation and hit
    catastrophic backtracking on multi-MB binaries — 300s+ hang). Streams are
    located with str.find (linear); text is pulled with the single-char-class
    paren-string regex (linear)."""
    import zlib
    data = open(path, "rb").read()
    out = []

    # linear stream scan
    positions = []
    i = 0
    marker = b"stream"
    while True:
        j = data.find(marker, i)
        if j == -1:
            break
        k = j + len(marker)
        if data[k:k + 2] == b"\r\n":
            k += 2
        elif data[k:k + 1] in (b"\n", b"\r"):
            k += 1
        end = data.find(b"endstream", k)
        if end == -1:
            break
        positions.append((k, end))
        i = end + 9

    for k, end in positions:
        raw = data[k:end]
        try:
            raw = zlib.decompress(raw)
        except Exception:
            pass
        # linear: all PDF paren-strings in this content stream (text-showing)
        for m in re.finditer(rb"\((?:[^()\\]|\\.)*\)", raw):
            line = m.group(0)[1:-1]
            try:
                text = line.decode("utf-8", "replace")
            except UnicodeDecodeError:
                text = line.decode("latin-1", "replace")
            text = text.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
            # GARBAGE FILTER (earned the hard way): content streams of
            # scanned/image-heavy PDFs contain paren-strings of raw binary;
            # without this filter a 350KB mislabeled pptx yields ~350K chars
            # of \x00\x10 junk that poisons the concept graph.
            if not text.strip():
                continue
            printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
            if printable / len(text) < 0.85:
                continue
            letters = sum(1 for ch in text if ch.isalpha() or ch.isspace())
            if letters / len(text) < 0.5:
                continue
            out.append(text.strip())
    if not out:
        return "(no extractable text — likely scanned; route to OCR/vision adapter)"
    return "\n".join(out)


# ---------------------------------------------------------------- dispatcher

def extract(path):
    """Return (kind, text or None). kind in: text, image, media, binary.
    Routes by magic bytes FIRST (course downloads mislabel files), then by
    extension for the true zip/xml formats."""
    ext = os.path.splitext(path)[1].lower()
    if ext in MEDIA_EXT:
        return "media", None
    if ext in IMAGE_EXT:
        return "image", None
    magic = sniff(path)
    try:
        # magic byte check wins: a .docx that is really a PDF parses as PDF
        if magic == "pdf":
            return "text", read_pdf(path)
        if magic == "text":
            return "text", read_plain(path)
        if magic in ("zip", "binary") and ext == ".svg":
            # svg is XML not zip; sniff says 'text' for ascii svg but handle
            # both — guard on magic when extension disagrees
            if magic == "text":
                return "text", read_svg(path)
        if ext in (".txt", ".md", ".markdown", ".log", ".rst", ""):
            return "text", read_plain(path)
        if ext == ".csv":
            return "text", read_csv_like(path, ",")
        if ext == ".tsv":
            return "text", read_csv_like(path, "\t")
        if ext == ".json":
            return "text", read_json(path)
        if ext in (".html", ".htm"):
            return "text", read_html(path)
        if ext in (".srt", ".vtt"):
            return "text", read_srt_vtt(path)
        if ext == ".svg":
            return "text", read_svg(path)
        if ext in (".xlsx", ".xlsm"):
            return "text", read_xlsx(path)
        if ext == ".docx":
            return "text", read_docx(path)
        if ext == ".pptx":
            return "text", read_pptx(path)
        if ext == ".epub":
            return "text", read_epub(path)
        if ext in EBOOK_HINT:
            return "binary", None
        if ext == ".pdf":
            return "text", read_pdf(path)
        if ext == ".odt":
            z = zipfile.ZipFile(path)
            return "text", _xml_text(z.read("content.xml"))
    except Exception as e:
        return "binary", f"(extraction failed: {e})"
    return "binary", None


def iter_files(root, recursive):
    if os.path.isfile(root):
        return [root]
    if not recursive:
        return [os.path.join(root, f) for f in sorted(os.listdir(root))
                if os.path.isfile(os.path.join(root, f))]
    acc = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRNAMES]
        for f in sorted(fn):
            acc.append(os.path.join(dp, f))
    return acc


def main():
    ap = argparse.ArgumentParser(description="Extract text from any course file format")
    ap.add_argument("path", help="file or directory")
    ap.add_argument("--out-dir", default=None, help="where normalized .txt files land (default: <path>/extracted)")
    ap.add_argument("--recursive", action="store_true")
    a = ap.parse_args()

    files = iter_files(a.path, a.recursive)
    if not files:
        sys.exit(f"FATAL: nothing to ingest at {a.path}")

    out_dir = a.out_dir or (os.path.join(a.path, "extracted") if os.path.isdir(a.path)
                           else os.path.join(os.path.dirname(a.path), "extracted"))
    os.makedirs(out_dir, exist_ok=True)

    corpus_path = os.path.join(out_dir, "corpus.json")
    corpus = {"files": []}
    if os.path.exists(corpus_path):
        try:
            corpus = json.load(open(corpus_path))
        except Exception:
            corpus = {"files": []}

    # ATTRIBUTION (natural inbuilt): pull url/credential identity/timestamp
    # from any acquisition.json in the source tree so every downstream record
    # — corpus.json, the graph, queries, exports — carries full provenance.
    # NOTE: the walk var is _dirs (not files) — shadowing `files` here once
    # silently emptied the ingest loop (files=0 with a file present).
    acq = {}
    for root, _dirs, _fnames in os.walk(os.path.dirname(os.path.abspath(a.path)) or "."):
        _dirs[:] = [d for d in _dirs if d not in SKIP_DIRNAMES]
        apath = os.path.join(root, "acquisition.json")
        if os.path.exists(apath):
            try:
                for rec in json.load(open(apath)).get("sources", []):
                    acq[os.path.abspath(rec.get("file", ""))] = rec
            except Exception:
                pass

    counts = {"text": 0, "media": 0, "image": 0, "binary": 0}
    used_stems = set()
    for path in files:
        rel = os.path.relpath(path, os.path.dirname(a.path) if os.path.isdir(a.path) else ".")
        entry = {"file": os.path.abspath(path), "rel": rel, "sha256": sha256_file(path),
                 "size": os.path.getsize(path),
                 "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        # merge acquisition attribution when this file was acquired (url,
        # credential identity, acquired_at, tool)
        arec = acq.get(os.path.abspath(path))
        if arec:
            for k in ("url", "credential", "acquired_at", "tool", "kind"):
                if arec.get(k) and k not in entry:
                    entry[k] = arec[k]
        kind, text = extract(path)
        entry["kind"] = kind
        if kind == "text" and text is not None:
            # unique stem per source file: keep full rel-path shape so
            # "sample.csv" and "lesson/sample.csv" never collide (a flat
            # stem let six formats overwrite each other — silent data loss)
            stem = re.sub(r"[^a-zA-Z0-9._-]", "_", rel)
            if stem.endswith(".txt"):
                stem = stem[:-4]
            base = stem
            n = 1
            while stem in used_stems:
                n += 1
                stem = f"{base}.{n}"
            used_stems.add(stem)
            tpath = os.path.join(out_dir, stem + ".txt")
            with open(tpath, "w") as f:
                f.write(text)
            entry["extracted_to"] = tpath
            entry["chars"] = len(text)
        elif kind == "image":
            entry["needs_ocr"] = True
        elif kind == "media":
            entry["needs_transcription"] = True
        elif kind == "binary" and os.path.splitext(path)[1].lower() in EBOOK_HINT:
            entry["hint"] = EBOOK_HINT[os.path.splitext(path)[1].lower()]
        counts[kind] += 1
        corpus["files"].append(entry)

    with open(corpus_path, "w") as f:
        json.dump(corpus, f, indent=2)

    print(json.dumps({"ok": True, "files": len(files), **counts, "corpus": corpus_path,
                      "out_dir": out_dir,
                      "files_list": [{"file": e["file"], "kind": e["kind"],
                                      "needs_transcription": e.get("needs_transcription", False),
                                      "needs_ocr": e.get("needs_ocr", False)}
                                     for e in corpus["files"]]}, indent=2))


if __name__ == "__main__":
    main()