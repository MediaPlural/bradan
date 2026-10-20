#!/usr/bin/env python3
"""bradan sync — the other half of syncable-anywhere: import + merge.

export.py pushes the graph out; sync.py brings graphs IN and merges them:

  python3 sync.py merge LEFT.db RIGHT.db --out merged.db
      Merge two bradan graphs (union; chunk-level dedup by exact text
      hash; concepts re-canonicalized). Re-importing the same db is a no-op.

  python3 sync.py import-jsonl chunks.jsonl --db TARGET.db
      Import a chunks.jsonl (ours or any producer of that shape) as new
      sources/chunks — vectors re-derived deterministically (same text =
      same vector, so cross-graph cosine works without shipping models).

  python3 sync.py import-package SHARE_DIR --db TARGET.db
      Import a full export.py --format package dir (reads graph.json; the
      INGEST.md convention flows both directions).

Sync path between boxes: export package on A → move the dir (scp, gdrive,
usb, carrier pigeon) → import-package on B. Or run api.py on A and any
tool on B reads it live. No cloud, no lock-in, no central authority needed.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graph as g  # noqa: E402


def text_hash(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def db_graph_rows(db):
    sources = {r[0]: r for r in db.execute("SELECT id, name, kind, path, sha256 FROM sources")}
    chunks = {text_hash(r[0]): (r[0], r[1], r[2]) for r in db.execute(
        "SELECT text, source_id, idx FROM chunks")}
    concepts = {r[1]: r[0] for r in db.execute("SELECT id, phrase FROM concepts")}
    return sources, chunks, concepts


def absorb(target_path, donor_path=None, jsonl_path=None):
    db, has_fts = g.connect(target_path)
    _, my_chunks, _ = db_graph_rows(db)
    added = {"sources": 0, "chunks": 0, "concepts": 0}

    def add_source(name, kind, path, sha):
        cur = db.execute("INSERT INTO sources(name,kind,path,sha256,meta_json) VALUES(?,?,?,?,?)",
                         (name, kind or "text", path, sha, "{}"))
        return cur.lastrowid

    def add_chunks(text, sid, meta=None):
        h = text_hash(text)
        if h in my_chunks:
            return 0
        my_chunks[h] = True
        for i, chunk in enumerate(g.chunk_text(text)):
            vec = g.embed(chunk)
            cur = db.execute("INSERT INTO chunks(source_id,idx,text,words,vector_json,meta_json) "
                             "VALUES(?,?,?,?,?,?)",
                             (sid, i, chunk, len(chunk.split()), json.dumps(vec), "{}"))
            rid = cur.lastrowid
            if has_fts:
                db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (rid, chunk))
            local = {}
            for w in [w for w in __import__("re").findall(r"[a-z][a-z'-]{2,}", chunk.lower())
                      if w not in g.STOP]:
                local[w] = local.get(w, 0) + 1
            for w, tf in local.items():
                cid = g.concept_id(db, w)
                db.execute("INSERT INTO chunk_concepts VALUES(?,?,?)", (rid, cid, tf))
            added["chunks"] += 1
        return 1

    if jsonl_path:
        # group lines by their source field: one source row per source, not
        # per line (the live round-trip caught 223 sources for 223 chunks)
        src_map = {}
        for line in open(jsonl_path, errors="replace"):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            name = d.get("source", "imported")
            if name not in src_map:
                sid = add_source(name, d.get("kind"), jsonl_path, d.get("source_sha256"))
                src_map[name] = sid
                added["sources"] += 1
            add_chunks(d["text"], src_map[name])

    if donor_path:
        donor, _ = g.connect(donor_path)
        donor.row_factory = sqlite3.Row
        for s in donor.execute("SELECT id, name, kind, path, sha256 FROM sources"):
            sid = add_source(s["name"], s["kind"], s["path"], s["sha256"])
            added["sources"] += 1
            for c in donor.execute("SELECT text FROM chunks WHERE source_id=? ORDER BY idx", (s["id"],)):
                add_chunks(c["text"], sid)
        donor.close()

    db.commit()
    g.build_links(db)
    stats = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
             for t in ["sources", "chunks", "concepts", "bridges"]}
    added.update({"after": stats, "db": os.path.abspath(target_path)})
    print(json.dumps({"ok": True, **added}, indent=2))
    db.close()


def merge(left, right, out):
    if os.path.abspath(left) == os.path.abspath(out) or os.path.abspath(right) == os.path.abspath(out):
        sys.exit("FATAL: --out must be a new path")
    if os.path.exists(out):
        os.remove(out)
    shutil.copy2(left, out)
    absorb(out, donor_path=right)


def import_package(pkg_dir, target):
    gj = os.path.join(pkg_dir, "graph.json")
    if not os.path.exists(gj):
        sys.exit(f"FATAL: {pkg_dir} has no graph.json — not a bradan package")
    # package -> temp jsonl -> import (one code path, honest formats)
    data = json.load(open(gj))
    tmp = os.path.join(os.path.dirname(target) or ".", ".pkg-import.jsonl")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    src_names = {s["id"]: s["name"] for s in data.get("sources", [])}
    with open(tmp, "w") as f:
        for c in data.get("chunks", []):
            f.write(json.dumps({"text": c["text"], "source": src_names.get(c["source_id"], "package"),
                                "kind": "text"}, ensure_ascii=False) + "\n")
    if not os.path.exists(target):
        # first import into a fresh db: create schema via a 0-row ingest? simpler: connect
        db, _ = g.connect(target)
        db.close()
    absorb(target, jsonl_path=tmp)
    os.remove(tmp)


def main():
    ap = argparse.ArgumentParser(description="Import/merge graphs — syncable anywhere")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("merge")
    p1.add_argument("left"); p1.add_argument("right"); p1.add_argument("--out", required=True)
    p2 = sub.add_parser("import-jsonl")
    p2.add_argument("jsonl"); p2.add_argument("--db", required=True)
    p3 = sub.add_parser("import-package")
    p3.add_argument("pkg_dir"); p3.add_argument("--db", required=True)
    a = ap.parse_args()
    if a.cmd == "merge":
        merge(a.left, a.right, a.out)
    elif a.cmd == "import-jsonl":
        absorb(a.db, jsonl_path=a.jsonl)
    elif a.cmd == "import-package":
        import_package(a.pkg_dir, a.db)


if __name__ == "__main__":
    main()