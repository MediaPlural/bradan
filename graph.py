#!/usr/bin/env python3
"""bradan graph — the assimilated, queryable knowledge graph.

The engine of enlightenment: everything consumed lands here — every chunk of
text from every source (websites, zips, gdrive, courses, PDFs, transcripts,
spreadsheets, decks) — vectorized, cross-linked, and queryable five ways:

  search      keyword/FTS (with LIKE fallback when sqlite lacks FTS5)
  semantic    cosine similarity over deterministic hashed embeddings
  filter      source / kind / date / any metadata facet
  insight     cross-source bridges, shared-concept ranks, orphan detection —
              the discovery layer: what INTEGRATION makes visible that no
              single source says
  maths       corpus statistics: counts, densities, distributions

Design laws (house):
  - stdlib-only. sqlite3 + hashlib + math. No API keys, no cloud.
  - deterministic embeddings: token n-grams -> fixed-dim hashed vector (the
    hashing trick). Same text = same vector, forever; no model download.
  - provenance-anchored: every chunk carries source file + sha256 + kind.
  - discovery over recall: the insight query surfaces CROSS-SOURCE structure
    (bridges), because new synthesis lives between sources, not inside one.

DB schema (consumer.graph.db):
  sources(id, name, kind, path, sha256, meta_json)
  chunks(id, source_id, idx, text, words, vector_json, meta_json)
  concepts(id, phrase)
  chunk_concepts(chunk_id, concept_id, tf)
  concept_links(a_id, b_id, co)          -- concept co-occurrence
  bridges(concept_id, source_count)      -- concepts spanning >=2 sources

Usage:
  python3 graph.py ingest CORPUS_DIR [--distilled DISTILLED] [--db PATH]
  python3 graph.py search "query" [--limit 10]
  python3 graph.py semantic "query" [--limit 10]
  python3 graph.py hybrid "query" [--limit 10]     (FTS+semantic fused)
  python3 graph.py filter [--source X] [--kind Y] [--limit 20]
  python3 graph.py insight [--bridges 15] [--orphans]
  python3 graph.py maths
  python3 graph.py status
"""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time

DIM = 512          # hashed embedding dimension
CHUNK_CHARS = 900  # chunk size
CHUNK_OVERLAP = 150
STOP = set("""a an the and or but if then else for of to in on at by with from as is are was were
be been being this that these those it its we you your our their they them he she i me my
do does did done have has had will would can could should shall may might must not no nor
so than too very just about into over under out up down off again further once here there
when where why how all any both each few more most other some such only own same what which
who whom also like okay yeah um""".split())


# ------------------------------------------------------------------ vectors

def embed(text):
    """Deterministic hashed embedding: unigram+bigram tokens -> sparse vector,
    L2-normalized. Same text always yields the same vector (no model, no RNG)."""
    words = [w for w in re.findall(r"[a-z][a-z'-]{1,}", text.lower()) if w not in STOP]
    if not words:
        return {}
    grams = words + [f"{words[i]}_{words[i+1]}" for i in range(len(words) - 1)]
    vec = {}
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little")
        idx = h % DIM
        sign = 1.0 if (h >> 63) & 1 else -1.0
        vec[idx] = vec.get(idx, 0.0) + sign
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {str(k): round(v / norm, 5) for k, v in vec.items()}


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


# ------------------------------------------------------------------ chunking

def chunk_text(text):
    """Yield overlapping chunks that respect sentence boundaries when possible."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            # back off to a sentence/word boundary
            cut = text.rfind(". ", start, end)
            if cut == -1 or cut < start + CHUNK_CHARS // 2:
                cut = text.rfind(" ", start, end)
            if cut > start:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


# ------------------------------------------------------------------ schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, name TEXT, kind TEXT, path TEXT,
  sha256 TEXT, meta_json TEXT, url TEXT, credential TEXT,
  acquired_at TEXT, acquire_tool TEXT);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, source_id INTEGER, idx INTEGER,
  text TEXT, words INTEGER, vector_json TEXT, meta_json TEXT);
CREATE TABLE IF NOT EXISTS concepts(
  id INTEGER PRIMARY KEY, phrase TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS chunk_concepts(
  chunk_id INTEGER, concept_id INTEGER, tf INTEGER);
CREATE TABLE IF NOT EXISTS concept_links(
  a_id INTEGER, b_id INTEGER, co INTEGER);
CREATE TABLE IF NOT EXISTS bridges(
  concept_id INTEGER PRIMARY KEY, source_count INTEGER);
CREATE INDEX IF NOT EXISTS ix_chunks_src ON chunks(source_id);
CREATE INDEX IF NOT EXISTS ix_cc_chunk ON chunk_concepts(chunk_id);
CREATE INDEX IF NOT EXISTS ix_cc_concept ON chunk_concepts(concept_id);
"""


def connect(db_path):
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    try:
        # contentful FTS (not content=''): snippet() needs indexed text to
        # render highlights; contentless tables return null snippets (caught
        # live: search hits came back with text:null)
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)")
        has_fts = True
    except sqlite3.OperationalError:
        has_fts = False
    return db, has_fts


# ------------------------------------------------------------------ ingest

def load_corpus(corpus_dir):
    """corpus.json (from ingest.py) + extracted .txt files. Attribution
    (url/credential/acquired_at/tool) flows from corpus.json entries into
    the graph's source rows — natural inbuilt provenance."""
    entries = []
    cj = os.path.join(corpus_dir, "corpus.json")
    if os.path.exists(cj):
        for e in json.load(open(cj)).get("files", []):
            if e.get("extracted_to") and os.path.exists(e["extracted_to"]):
                entries.append({"name": e.get("rel") or os.path.basename(e["file"]),
                                "kind": e.get("kind", "text"),
                                "path": e["file"], "sha256": e.get("sha256"),
                                "text_path": e["extracted_to"],
                                "url": e.get("url"),
                                "credential": e.get("credential"),
                                "acquired_at": e.get("acquired_at"),
                                "tool": e.get("tool"),
                                "source_size": e.get("size")})
    else:
        # bare dir of .txt/.md (transcripts, notes) — graph them too
        for f in sorted(os.listdir(corpus_dir)):
            if f.lower().endswith((".txt", ".md")):
                p = os.path.join(corpus_dir, f)
                entries.append({"name": f, "kind": "text", "path": p,
                                "sha256": None, "text_path": p})
    return entries


def purge_source(db, has_fts, sid):
    """Remove a source and everything it contributed (chunks, fts rows,
    chunk-concept edges). Concepts stay (shared); links/bridges rebuilt after."""
    cids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE source_id=?", (sid,))]
    if cids:
        marks = ",".join("?" * len(cids))
        db.execute(f"DELETE FROM chunk_concepts WHERE chunk_id IN ({marks})", cids)
        if has_fts:
            db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", cids)
        db.execute(f"DELETE FROM chunks WHERE id IN ({marks})", cids)
    db.execute("DELETE FROM sources WHERE id=?", (sid,))


def ingest(db_path, corpus_dir, distilled_dir=None):
    db, has_fts = connect(db_path)
    entries = load_corpus(corpus_dir)
    if not entries:
        sys.exit(f"FATAL: nothing to graph in {corpus_dir} (expected corpus.json + extracted/, or *.txt/*.md)")

    # concepts from the distiller when available (else derive from text below)
    concepts = {}
    if distilled_dir and os.path.exists(os.path.join(distilled_dir, "concepts.json")):
        for c in json.load(open(os.path.join(distilled_dir, "concepts.json"))).get("concepts", []):
            concepts[c["phrase"].lower()] = c["phrase"]

    # IDEMPOTENCY (the re-point law): a source ingests once; pointing at the
    # same material twice must never duplicate it. Same name + same sha256 =
    # skip (unchanged). Same name + different sha = purge + re-add (changed).
    existing = {r[0]: (r[1], r[2]) for r in
                db.execute("SELECT name, id, sha256 FROM sources")}
    src_count, chunk_count, skipped = 0, 0, 0
    for e in entries:
        text = open(e["text_path"], errors="replace").read()
        if not text.strip():
            continue
        name, sha = e["name"], e.get("sha256")
        if name in existing:
            old_sid, old_sha = existing[name]
            if sha and old_sha and sha == old_sha:
                skipped += 1
                continue
            purge_source(db, has_fts, old_sid)
        elif sha:
            # content-addressed: if the same sha exists under another name
            # (a re-export with a different filename), still skip the dupe
            dupe = db.execute("SELECT id FROM sources WHERE sha256=? AND kind=?",
                              (sha, e.get("kind", "text"))).fetchone()
            if dupe:
                skipped += 1
                continue
        cur = db.execute("INSERT INTO sources(name,kind,path,sha256,meta_json,url,credential,acquired_at,acquire_tool) "
                         "VALUES(?,?,?,?,?,?,?,?,?)",
                         (name, e["kind"], e["path"], sha,
                          json.dumps({"text_path": e["text_path"]}),
                          e.get("url"), e.get("credential"),
                          e.get("acquired_at"), e.get("tool")))
        sid = cur.lastrowid
        existing[name] = (sid, sha)
        src_count += 1
        for i, chunk in enumerate(chunk_text(text)):
            vec = embed(chunk)
            words = len(chunk.split())
            cur = db.execute("INSERT INTO chunks(source_id,idx,text,words,vector_json,meta_json) "
                             "VALUES(?,?,?,?,?,?)",
                             (sid, i, chunk, words, json.dumps(vec), "{}"))
            chunk_rowid = cur.lastrowid
            if has_fts:
                db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (chunk_rowid, chunk))
            chunk_count += 1
            # chunk-level concepts (derive if distiller absent)
            local = {}
            wl = [w for w in re.findall(r"[a-z][a-z'-]{2,}", chunk.lower()) if w not in STOP]
            for w in wl:
                local[w] = local.get(w, 0) + 1
            for w, tf in local.items():
                phrase = concepts.get(w) or w
                cid = concept_id(db, phrase)
                db.execute("INSERT INTO chunk_concepts VALUES(?,?,?)", (chunk_rowid, cid, tf))
    db.commit()
    build_links(db)
    print(json.dumps({"ok": True, "sources": src_count, "chunks": chunk_count,
                      "skipped": skipped,
                      "concepts": db.execute("SELECT count(*) FROM concepts").fetchone()[0],
                      "fts5": has_fts, "db": os.path.abspath(db_path)}, indent=2))
    db.close()


def concept_id(db, phrase):
    cur = db.execute("INSERT OR IGNORE INTO concepts(phrase) VALUES(?)", (phrase,))
    return db.execute("SELECT id FROM concepts WHERE phrase=?", (phrase,)).fetchone()[0]


def build_links(db):
    """Concept co-occurrence within chunks + cross-source bridges."""
    db.execute("DELETE FROM concept_links")
    db.execute("DELETE FROM bridges")
    rows = db.execute("""
        SELECT cc1.concept_id a, cc2.concept_id b, count(*) co
        FROM chunk_concepts cc1 JOIN chunk_concepts cc2
          ON cc1.chunk_id = cc2.chunk_id AND cc1.concept_id < cc2.concept_id
        GROUP BY a, b HAVING co >= 3""").fetchall()
    for a, b, co in rows[:20000]:
        db.execute("INSERT INTO concept_links VALUES(?,?,?)", (a, b, co))
    for cid, n in db.execute("""
            SELECT cc.concept_id, count(DISTINCT c.source_id) n
            FROM chunk_concepts cc JOIN chunks c ON cc.chunk_id = c.id
            GROUP BY cc.concept_id HAVING n >= 2""").fetchall():
        db.execute("INSERT OR REPLACE INTO bridges VALUES(?,?)", (cid, n))
    db.commit()


# ------------------------------------------------------------------ queries


def _attribution(db):
    """id -> attribution dict for result rows. Natural inbuilt provenance:
    every query result carries where the knowledge came from."""
    return {r[0]: {"url": r[1], "credential": r[2], "acquired_at": r[3], "tool": r[4], "sha256": r[5]}
            for r in db.execute("SELECT id, url, credential, acquired_at, acquire_tool, sha256 FROM sources")}


def q_search(db, has_fts, query, limit):
    if has_fts:
        try:
            # fts5 rowid == chunks.id (inserted 1:1); snippet from the fts table
            rows = db.execute("""
                SELECT c.id, s.name, snippet(chunks_fts, 0, '[', ']', '…', 12),
                       bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN sources s ON s.id = c.source_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank LIMIT ?""",
                (query, limit)).fetchall()
            att = _attribution(db)
            # rows already carry source ids via join; map attribution cleanly
            sid_of = dict(db.execute("SELECT id, source_id FROM chunks").fetchall())
            return [{"chunk": r[0], "source": r[1],
                     "attribution": att.get(sid_of.get(r[0])),
                     "text": r[2], "fts_rank": round(r[3], 3)} for r in rows]
        except sqlite3.OperationalError:
            pass
    like = f"%{query}%"
    rows = db.execute("""SELECT c.id, s.name, s.id, substr(c.text,1,240),
                                 s.url, s.credential, s.acquired_at, s.acquire_tool, s.sha256
                         FROM chunks c
                         JOIN sources s ON s.id=c.source_id
                         WHERE c.text LIKE ? LIMIT ?""", (like, limit)).fetchall()
    return [{"chunk": r[0], "source": r[1],
             "attribution": {"source_id": r[2], "url": r[4], "credential": r[5],
                             "acquired_at": r[6], "tool": r[7], "sha256": r[8]},
             "text": r[3], "fts_rank": None} for r in rows]


def q_semantic(db, query, limit):
    qv = embed(query)
    out = []
    for cid, sid, text, vj in db.execute("SELECT id, source_id, text, vector_json FROM chunks"):
        score = cosine(qv, json.loads(vj))
        if score > 0.02:
            out.append((score, cid, sid, text))
    out.sort(reverse=True)
    out = out[:limit]
    srcs = dict(db.execute("SELECT id, name FROM sources").fetchall())
    att = _attribution(db)
    return [{"chunk": c, "source": srcs.get(s), "similarity": round(sc, 3),
             "attribution": att.get(s),
             "text": t[:240]} for sc, c, s, t in out]


def q_hybrid(db, has_fts, query, limit):
    sem = {r["chunk"]: r for r in q_semantic(db, query, limit * 3)}
    fts = {r["chunk"]: r for r in q_search(db, has_fts, query, limit * 3)}
    ids = list(dict.fromkeys(list(sem) + list(fts)))
    srcs = dict(db.execute("SELECT id, name FROM sources").fetchall())
    att = _attribution(db)
    rows = db.execute("SELECT id, source_id, text FROM chunks").fetchall()
    texts = {r[0]: (r[1], r[2]) for r in rows}
    out = []
    for i in ids:
        s = sem.get(i, {}).get("similarity", 0.0)
        f = 1.0 if i in fts else 0.0
        score = s + f * (0.5 + s)   # fusion: both-signal chunks rank highest
        sid, text = texts.get(i, (None, ""))
        out.append({"chunk": i, "source": srcs.get(sid), "score": round(score, 3),
                    "similarity": round(s, 3), "fts_hit": bool(f), "text": text[:240],
                    "attribution": att.get(sid)})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


def q_filter(db, source=None, kind=None, limit=20):
    sql = ("SELECT c.id, s.name, s.kind, substr(c.text,1,200), c.words, s.id, "
           "s.url, s.credential, s.acquired_at, s.acquire_tool, s.sha256 "
           "FROM chunks c JOIN sources s ON s.id=c.source_id")
    where, args = [], []
    if source:
        where.append("s.name LIKE ?"); args.append(f"%{source}%")
    if kind:
        where.append("s.kind = ?"); args.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " LIMIT ?"; args.append(limit)
    rows = db.execute(sql, args).fetchall()
    return [{"chunk": r[0], "source": r[1], "kind": r[2], "text": r[3], "words": r[4],
             "attribution": {"source_id": r[5], "url": r[6], "credential": r[7],
                             "acquired_at": r[8], "tool": r[9], "sha256": r[10]}} for r in rows]


def q_insight(db, bridges=15, orphans=False):
    out = {}
    src_count = db.execute("SELECT count(*) FROM sources").fetchone()[0]
    rows = db.execute("""
        SELECT cp.phrase, b.source_count
        FROM bridges b JOIN concepts cp ON cp.id = b.concept_id
        ORDER BY b.source_count DESC, cp.phrase LIMIT ?""", (bridges,)).fetchall()
    out["bridge_concepts"] = [{"concept": r[0], "spans_sources": r[1],
                               "coverage": f"{r[1]}/{src_count}"} for r in rows]
    out["strongest_pairs"] = [{"a": db.execute("SELECT phrase FROM concepts WHERE id=?", (a,)).fetchone()[0],
                               "b": db.execute("SELECT phrase FROM concepts WHERE id=?", (b,)).fetchone()[0],
                               "co": co}
                              for a, b, co in db.execute(
                                  """SELECT a_id, b_id, co FROM concept_links
                                     ORDER BY co DESC LIMIT 10""").fetchall()]
    per_src = db.execute("""SELECT s.name, count(DISTINCT cc.concept_id) FROM sources s
                            JOIN chunks c ON c.source_id=s.id
                            JOIN chunk_concepts cc ON cc.chunk_id=c.id
                            GROUP BY s.name ORDER BY 2 DESC""").fetchall()
    out["concept_richness"] = [{"source": r[0], "distinct_concepts": r[1]} for r in per_src]
    if orphans:
        linked = {r[0] for r in db.execute(
            "SELECT DISTINCT source_id FROM chunks c JOIN chunk_concepts cc ON cc.chunk_id=c.id").fetchall()}
        names = dict(db.execute("SELECT id, name FROM sources").fetchall())
        out["orphan_sources"] = [names[i] for i in (set(names) - linked)]
    return out


def q_maths(db):
    out = {}
    out["sources"] = db.execute("SELECT count(*) FROM sources").fetchone()[0]
    out["chunks"] = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    out["concepts"] = db.execute("SELECT count(*) FROM concepts").fetchone()[0]
    out["bridges"] = db.execute("SELECT count(*) FROM bridges").fetchone()[0]
    w = db.execute("SELECT sum(words), avg(words), min(words), max(words) FROM chunks").fetchone()
    out["words_total"], out["words_per_chunk_avg"] = w[0], round(w[1], 1) if w[1] else 0
    out["words_min"], out["words_max"] = w[2], w[3]
    per = db.execute("""SELECT s.name, count(c.id), sum(c.words) FROM sources s
                        LEFT JOIN chunks c ON c.source_id=s.id GROUP BY s.name ORDER BY 3 DESC""").fetchall()
    out["per_source"] = [{"source": r[0], "chunks": r[1], "words": r[2]} for r in per]
    top = db.execute("""SELECT cp.phrase, sum(cc.tf) FROM chunk_concepts cc
                        JOIN concepts cp ON cp.id=cc.concept_id
                        GROUP BY cp.phrase ORDER BY 2 DESC LIMIT 15""").fetchall()
    out["top_concepts_by_tf"] = [{"concept": r[0], "tf": r[1]} for r in top]
    return out


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="The assimilated knowledge graph: search/semantic/filter/insight/maths")
    sub = ap.add_subparsers(dest="cmd", required=True)
    default_db = os.environ.get("BRADAN_GRAPH_DB", "consumer.graph.db")

    p = sub.add_parser("ingest"); p.add_argument("corpus_dir")
    p.add_argument("--distilled", default=None)
    p.add_argument("--db", default=default_db)

    for name, help_ in [("search", "keyword/FTS"), ("semantic", "cosine"), ("hybrid", "FTS+semantic fused")]:
        p = sub.add_parser(name, help=help_); p.add_argument("query"); p.add_argument("--limit", type=int, default=10)
        p.add_argument("--db", default=default_db)

    p = sub.add_parser("filter"); p.add_argument("--source", default=None)
    p.add_argument("--kind", default=None); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--db", default=default_db)

    p = sub.add_parser("insight"); p.add_argument("--bridges", type=int, default=15)
    p.add_argument("--orphans", action="store_true"); p.add_argument("--db", default=default_db)

    p = sub.add_parser("maths"); p.add_argument("--db", default=default_db)
    p = sub.add_parser("status"); p.add_argument("--db", default=default_db)
    a = ap.parse_args()

    if a.cmd == "ingest":
        ingest(a.db, a.corpus_dir, a.distilled)
        return
    if not os.path.exists(a.db):
        sys.exit(f"FATAL: no graph at {a.db} — run: graph.py ingest <corpus_dir>")
    db, has_fts = connect(a.db)
    if a.cmd == "search":
        res = q_search(db, has_fts, a.query, a.limit)
    elif a.cmd == "semantic":
        res = q_semantic(db, a.query, a.limit)
    elif a.cmd == "hybrid":
        res = q_hybrid(db, has_fts, a.query, a.limit)
    elif a.cmd == "filter":
        res = q_filter(db, a.source, a.kind, a.limit)
    elif a.cmd == "insight":
        res = q_insight(db, a.bridges, a.orphans)
    elif a.cmd == "maths":
        res = q_maths(db)
    else:
        res = q_maths(db)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    db.close()


if __name__ == "__main__":
    main()