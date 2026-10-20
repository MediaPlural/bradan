#!/usr/bin/env python3
"""consumer api — the graph over HTTP. API to everywhere.

Any tool, any language, any browser hits the same six queries the CLI does:

  GET /search?q=affiliate*&limit=5     FTS
  GET /semantic?q=payout+structures    cosine
  GET /hybrid?q=recurring+commission   fused
  GET /filter?kind=text&source=csv    facet filter
  GET /insight                         bridges + pairs + richness
  GET /maths                           corpus stats
  GET /export?format=jsonl             full dump (json|jsonl|md)
  POST /ingest {"corpus_dir": "..."}   rebuild the graph (localhost bind)

Default bind 127.0.0.1:8765 (localhost only — your data, your box; pass
--host 0.0.0.0 deliberately to share on the LAN). CORS open: agents and
browser tools can call it from anywhere you allow.

Usage:
  python3 api.py --db graph.db [--port 8765] [--host 127.0.0.1]
"""
import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graph as g  # noqa: E402
import export as ex  # noqa: E402

DB = None


def q(mode, params):
    db, has_fts = g.connect(DB)
    try:
        limit = int(params.get("limit", ["10"])[0])
        if mode == "search":
            return g.q_search(db, has_fts, params.get("q", [""])[0], limit)
        if mode == "semantic":
            return g.q_semantic(db, params.get("q", [""])[0], limit)
        if mode == "hybrid":
            return g.q_hybrid(db, has_fts, params.get("q", [""])[0], limit)
        if mode == "filter":
            return g.q_filter(db, params.get("source", [None])[0],
                              params.get("kind", [None])[0], limit)
        if mode == "insight":
            return g.q_insight(db, int(params.get("bridges", ["15"])[0]), "orphans" in params)
        if mode == "maths":
            return g.q_maths(db)
    finally:
        db.close()


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, indent=2, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlsplit(self.path)
        route = u.path.strip("/") or "maths"
        params = parse_qs(u.query)
        if route in ("search", "semantic", "hybrid", "filter", "insight", "maths"):
            try:
                return self._send(q(route, params))
            except Exception as e:
                return self._send({"error": str(e)}, 500)
        if route == "export":
            fmt = params.get("format", ["jsonl"])[0]
            db = ex.open_db(DB)
            try:
                if fmt == "jsonl":
                    path = "/tmp/consumer-api-export.jsonl"
                    res = ex.out_jsonl(db, path, None, None)
                elif fmt == "md":
                    path = "/tmp/consumer-api-export.md"
                    res = ex.out_md(db, path)
                else:
                    path = "/tmp/consumer-api-export.json"
                    res = ex.out_json(db, path, None, None)
                return self._send(res)
            finally:
                db.close()
        if route == "status":
            return self._send({"ok": True, "db": os.path.abspath(DB),
                               "endpoints": ["search", "semantic", "hybrid",
                                             "filter", "insight", "maths", "export"]})
        if route == "ui":
            body = open(os.path.join(HERE, "ui.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send({"error": f"unknown route: {route}"}, 404)

    def do_POST(self):
        if urlsplit(self.path).path.strip("/") != "ingest":
            return self._send({"error": "unknown route"}, 404)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            corpus_dir = body["corpus_dir"]
        except Exception:
            return self._send({"error": "POST /ingest needs JSON {corpus_dir}"}, 400)
        r = subprocess.run([sys.executable, os.path.join(HERE, "graph.py"), "ingest",
                            corpus_dir, "--db", DB],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            return self._send({"error": r.stderr[-400:]}, 500)
        return self._send(json.loads(r.stdout))


def main():
    global DB
    ap = argparse.ArgumentParser(description="The knowledge graph over HTTP — API to everywhere")
    ap.add_argument("--db", default=os.environ.get("BRADAN_GRAPH_DB", "consumer.graph.db"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 (default, local only) or 0.0.0.0 (LAN — deliberate)")
    a = ap.parse_args()
    if not os.path.exists(a.db):
        sys.exit(f"FATAL: no graph at {a.db} — run: graph.py ingest <corpus_dir> --db {a.db}")
    DB = a.db
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[api] bradan graph on http://{a.host}:{a.port}  db={os.path.abspath(DB)}",
          file=sys.stderr)
    print("[api] routes: /search /semantic /hybrid /filter /insight /maths /export /status",
          file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()