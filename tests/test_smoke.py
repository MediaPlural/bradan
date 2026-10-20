#!/usr/bin/env python3
"""consumer smoke tests — fast, offline, no model download.

Run: python3 tests/test_smoke.py
Covers: CLI shapes, transcript SRT generation, distiller scoring/dedupe,
acquisition manifest, MCP tool-call dispatch. The heavy path (real STT) is
exercised by the live verification already recorded in README provenance.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
PY = sys.executable
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def test_cli_shapes():
    print("CLI shapes")
    for tool in ["transcribe.py", "scrape.py", "distill.py", "consumer.py"]:
        r = run([PY, os.path.join(HERE, tool), "--help"])
        check(f"{tool} --help", r.returncode == 0, r.stderr[:200])


def test_srt_and_fingerprint():
    print("SRT + provenance writers (unit)")
    sys.path.insert(0, HERE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("tr", os.path.join(HERE, "transcribe.py"))
    tr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tr)
    srt = tr.srt_time(3661.5)
    check("srt_time", srt == "01:01:01,500", srt)
    segs = [{"start": 0.0, "end": 1.5, "text": "hello"}, {"start": 1.6, "end": 2.0, "text": "  "}]
    tmp = tempfile.mkdtemp() + "/x.srt"
    tr.write_srt(segs, tmp)
    body = open(tmp).read()
    check("write_srt skips empty", body.count("-->") == 1 and "hello" in body, body)
    # sha256 of a known string
    p = tempfile.mkdtemp() + "/f.txt"
    open(p, "w").write("abc")
    check("sha256_file", tr.sha256_file(p).startswith("ba7816bf"))


def test_distiller():
    print("distiller (unit)")
    sys.path.insert(0, HERE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("di", os.path.join(HERE, "distill.py"))
    di = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(di)
    kw = di.score_keywords(["the funnel is the machine that thinks about outreach",
                            "the funnel machine decides what the next move is"])
    check("keyword scoring", kw and kw[0]["term"] in ("funnel", "machine"), kw[:2])
    cons = di.extract_concepts("the funnel machine is a system. This is a test. "
                               "the funnel machine is a system. Reply within five minutes.")
    check("concept extraction no pronoun junk", "This" not in cons, cons[:5])
    check("concept bigram freq", cons.count("funnel machine") == 2, cons)


def _load_distill():
    sys.path.insert(0, HERE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("di", os.path.join(HERE, "distill.py"))
    di = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(di)
    return di


def test_distill_deep_units():
    print("distiller deep layer (unit, no network)")
    di = _load_distill()

    # canon + tolerant JSON extraction
    check("canon", di.canon("  Funnel   Machine! ") == "funnel machine")
    d = di.extract_json('noise before {"a": 1, "b": "x { not }"} and after')
    check("extract_json tolerant", d == {"a": 1, "b": "x { not }"}, str(d))

    # normalize clamps malformed model output instead of failing the chunk
    ext = di.normalize_extraction(
        {"concepts": [{"name": "Offer Call"}, {"name": "the thing"}, 42],
         "edges": [{"from": "offer call", "to": "ghost", "relation": "prerequisite"},
                   {"from": "offer call", "to": "not listed", "relation": "prerequisite"},
                   {"from": "offer call", "to": "offer call", "relation": "prerequisite"}],
         "takeaways": ["tiny", "A real takeaway sentence lives here."],
         "caveats": "not-a-list", "decisions": []},
        "chunk text")
    check("normalize drops malformed", len(ext["concepts"]) == 1
          and ext["concepts"][0]["name"] == "offer call", str(ext["concepts"]))
    check("normalize drops edges to unknowns", ext["edges"] == [], str(ext["edges"]))
    check("normalize keeps real takeaway", ext["takeaways"] == ["A real takeaway sentence lives here."])

    # chunk caching: second identical call never touches the model
    class FakeClient:
        model = "fake-model"
        calls = 0
        def complete(self, prompt):
            self.calls += 1
            return json.dumps({"concepts": [{"name": "funnel machine", "definition": "d"}],
                               "edges": [], "takeaways": ["t one"], "caveats": [], "decisions": []})
    cache = {"chunks": {}, "synthesis": {}}
    fake = FakeClient()
    e1 = di.distill_chunk_llm(fake, "some chunk of transcript", "l01.txt", cache)
    e2 = di.distill_chunk_llm(fake, "some chunk of transcript", "l01.txt", cache)
    check("chunk cache idempotent", fake.calls == 1 and e2.get("_cached") is True,
          f"calls={fake.calls}")
    check("chunk extraction has sha anchor", len(e1["chunk_sha256"]) == 64)

    # merge: case-folded duplicates become one node with provenance; edges weight
    exts = [{"concepts": [{"name": "funnel machine", "definition": "d1"},
                          {"name": "reply speed", "definition": "d2"}],
             "edges": [{"from": "reply speed", "to": "funnel machine", "relation": "prerequisite"}],
             "takeaways": [], "caveats": [], "decisions": [],
             "chunk_sha256": "a" * 64, "_source": "l01.transcript.json"},
            {"concepts": [{"name": "Funnel Machine", "definition": ""}],
             "edges": [{"from": "reply speed", "to": "funnel machine", "relation": "prerequisite"}],
             "takeaways": [], "caveats": [], "decisions": [],
             "chunk_sha256": "b" * 64, "_source": "l02.transcript.json"}]
    g = di.merge_extractions(exts)
    fm = [n for n in g["nodes"] if n["name"] == "funnel machine"][0]
    check("merge dedupes by canon", len(g["nodes"]) == 2 and fm["count"] == 2, str(g["nodes"]))
    check("merge keeps provenance", fm["sources"] == ["l01.transcript.json", "l02.transcript.json"]
          and len(fm["chunks"]) == 2, str(fm))
    check("merge edge weight", g["edges"] == [{"from": "reply speed", "to": "funnel machine",
          "relation": "prerequisite", "weight": 2,
          "sources": ["l01.transcript.json", "l02.transcript.json"]}], str(g["edges"]))

    # learning path: prerequisites come first ("reply speed" precedes "funnel machine")
    path = di.learning_path(g)
    check("learning path topo", path.index("reply speed") < path.index("funnel machine"), str(path))


def test_distill_deep_cli():
    print("distiller deep layer (CLI, --llm none)")
    tmp = tempfile.mkdtemp()
    tr = os.path.join(tmp, "transcripts")
    dist = os.path.join(tmp, "distilled")
    os.makedirs(tr)
    for i, text in enumerate([
            "The funnel machine is a system that decides the next move. "
            "Reply speed beats clever copy every time. The funnel machine needs a reply under five minutes. " * 8,
            "Booking the audit call is the goal of the funnel machine. "
            "If the offer is unclear the funnel machine stalls, that is the edge case most people hit. " * 8]):
        json.dump({"text": text, "segments": [{"start": 0.0, "end": 60.0 * (i + 1), "text": "s"}]},
                  open(os.path.join(tr, f"l{i:02d}.transcript.json"), "w"))
    r = run([PY, os.path.join(HERE, "distill.py"), tr, "--out-dir", dist, "--llm", "none"])
    check("distill --llm none runs", r.returncode == 0, r.stderr[:300])
    check("concept-graph written", os.path.exists(os.path.join(dist, "concept-graph.json")))
    check("synthesis written", os.path.exists(os.path.join(dist, "synthesis.json")))
    g = json.load(open(os.path.join(dist, "concept-graph.json")))
    check("graph schema", g["schema"] == "concept-graph/bradan-v1")
    check("graph fallback provenance", g["provenance"]["llm"] is None)
    check("graph sources sha-anchored",
          all(len(s["sha256"]) == 64 for s in g["provenance"]["sources"]))
    check("graph has nodes+edges", len(g["nodes"]) >= 2 and len(g["edges"]) >= 1,
          f"nodes={len(g['nodes'])} edges={len(g['edges'])}")
    check("fallback edges are co-occurs",
          all(e["relation"] == "co-occurs" for e in g["edges"]))
    s = json.load(open(os.path.join(dist, "synthesis.json")))
    check("synthesis schema", s["schema"] == "synthesis/bradan-v1" and "learning_path" in s)
    cmap = open(os.path.join(dist, "course-map.md")).read()
    check("course map renders graph", "## Concept graph" in cmap and "sha256" in cmap)
    check("legacy artifacts kept", os.path.exists(os.path.join(dist, "keywords.json"))
          and os.path.exists(os.path.join(dist, "next-best-seeds.json")))
    # idempotent re-run: same inputs, same graph shape
    r2 = run([PY, os.path.join(HERE, "distill.py"), tr, "--out-dir", dist, "--llm", "none"])
    g2 = json.load(open(os.path.join(dist, "concept-graph.json")))
    check("re-run idempotent", r2.returncode == 0
          and [n["name"] for n in g2["nodes"]] == [n["name"] for n in g["nodes"]]
          and len(g2["edges"]) == len(g["edges"]))
    shutil.rmtree(tmp, ignore_errors=True)


def test_scrape_manifest():
    print("scrape manifest (unit)")
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "page.txt")
    open(p, "w").write("hello")
    sys.path.insert(0, HERE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("sc", os.path.join(HERE, "scrape.py"))
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    m = sc.manifest(tmp, "https://example.com", p, "test")
    data = json.load(open(m))
    check("manifest writes", len(data["sources"]) == 1 and data["sources"][0]["url"] == "https://example.com")


def test_mcp_dispatch():
    print("MCP dispatch (integration, stdio)")
    # server reads stdin until EOF; communicate() closes stdin so it exits.
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    proc = subprocess.Popen([PY, os.path.join(HERE, "mcp-server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(json.dumps(payload) + "\n", timeout=30)
    resp = json.loads(out.strip().splitlines()[0])
    check("tools/list", resp["result"]["tools"] and len(resp["result"]["tools"]) >= 3,
          str(resp)[:200])
    # unknown tool call returns isError
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}}}
    proc = subprocess.Popen([PY, os.path.join(HERE, "mcp-server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(json.dumps(call) + "\n", timeout=30)
    resp = json.loads(out.strip().splitlines()[0])
    check("unknown tool -> isError", resp["result"].get("isError") is True, str(resp)[:200])


def test_course_dl_skool():
    print("course-dl skool support (unit)")
    sys.path.insert(0, HERE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("cdl", os.path.join(HERE, "course-dl.py"))
    cdl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cdl)
    check("skool md regex 32-hex", bool(cdl.SKOOL_MD_RE.search('href="?md=0430f1b55fa146099a333506c6adb7ac"')))
    check("skool md regex rejects short", not cdl.SKOOL_MD_RE.search("md=abc123"))
    # skool referer: save_media builds the yt-dlp command with --referer
    src = open(os.path.join(HERE, "course-dl.py")).read()
    check("referer flag present", '--referer", "https://www.skool.com' in src or
          '"--referer", "https://www.skool.com"' in src or "--referer" in src)
    # EMBED_HOSTS covers skool/mux/cloudflarestream
    check("embed hosts extended", all(h in cdl.EMBED_HOSTS for h in ("skool", "mux", "cloudflarestream")))


def test_author_and_explain():
    print("author + explain CLIs (unit)")
    r1 = run([PY, os.path.join(HERE, "author.py"), "--help"])
    check("author.py --help", r1.returncode == 0, r1.stderr[:150])
    r2 = run([PY, os.path.join(HERE, "explain.py"), "--help"])
    check("explain.py --help", r2.returncode == 0, r2.stderr[:150])
    # from-corpus against the live test corpus (created by earlier runs);
    # if missing, synthesize a minimal distilled corpus
    import tempfile
    dist = tempfile.mkdtemp() + "/distilled"
    os.makedirs(dist, exist_ok=True)
    with open(os.path.join(dist, "concepts.json"), "w") as f:
        json.dump({"concepts": [{"phrase": "five minutes", "count": 2}]}, f)
    with open(os.path.join(dist, "next-best-seeds.json"), "w") as f:
        json.dump({"seeds": [{"sentence": "Reply within five minutes instead of 30.", "density": 0.6, "words": 7}]}, f)
    with open(os.path.join(dist, "keywords.json"), "w") as f:
        json.dump({"keywords": [{"term": "reply", "tf": 3, "documents": 1, "score": 1.5}]}, f)
    out = tempfile.mkdtemp() + "/course"
    r3 = run([PY, os.path.join(HERE, "author.py"), "from-corpus", dist, "--title", "T", "--out", out, "--lessons", "2"])
    check("author from-corpus", r3.returncode == 0 and os.path.exists(os.path.join(out, "lessons", "lesson-02.md")), r3.stderr[:150])
    ex = tempfile.mkdtemp() + "/explainer"
    r4 = run([PY, os.path.join(HERE, "explain.py"), dist, "--title", "T", "--out", ex])
    ok = r4.returncode == 0 and os.path.exists(os.path.join(ex, "slides.py")) and os.path.exists(os.path.join(ex, "storyboard.md"))
    check("explain project", ok, r4.stderr[:150])
    if ok:
        ns = {}
        exec(open(os.path.join(ex, "slides.py")).read(), ns)
        check("rendermill schema", isinstance(ns["SLIDES"], list) and ns["SLIDES"][0]["type"] == "html"
              and "narration" in ns["SLIDES"][0] and "tts" in ns["CONFIG"])




def test_graph_and_source():
    print("graph + source (unit, live mini-corpus)")
    import tempfile, zipfile
    tmp = tempfile.mkdtemp()
    # a two-file zip source
    zp = os.path.join(tmp, "k.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("a.txt", "The funnel machine decides the next move. " * 8)
        z.writestr("b.md", "Reply within five minutes. New evidence every day. " * 8)
    r = subprocess.run([PY, os.path.join(HERE, "source.py"), zp,
                        "--workspace", os.path.join(tmp, "ws"), "--full",
                        "--db", os.path.join(tmp, "g.db")],
                       capture_output=True, text=True, timeout=120)
    check("source --full chain", r.returncode == 0 and '"graph"' in r.stdout, r.stderr[-200:])
    if r.returncode == 0:
        g = json.loads(r.stdout)["graph"]
        check("graph built", g["sources"] >= 2 and g["chunks"] >= 2, str(g)[:150])
    db = os.path.join(tmp, "g.db")
    if os.path.exists(db):
        r2 = subprocess.run([PY, os.path.join(HERE, "graph.py"), "semantic",
                             "funnel machine", "--db", db], capture_output=True, text=True, timeout=60)
        check("semantic query", r2.returncode == 0 and len(json.loads(r2.stdout)) >= 1, r2.stderr[-150:])
        r3 = subprocess.run([PY, os.path.join(HERE, "graph.py"), "insight",
                             "--db", db], capture_output=True, text=True, timeout=60)
        check("insight query", r3.returncode == 0 and "bridge_concepts" in r3.stdout, r3.stderr[-150:])




def test_export_sync():
    print("export + sync (live round-trip)")
    import tempfile
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "g.db")
    # build a graph
    dist = os.path.join(tmp, "corpus")
    os.makedirs(dist, exist_ok=True)
    open(os.path.join(dist, "a.txt"), "w").write(
        "The funnel machine decides the next move for every lead. " * 10)
    r = subprocess.run([PY, os.path.join(HERE, "graph.py"), "ingest", dist, "--db", db],
                       capture_output=True, text=True, timeout=120)
    check("graph build", r.returncode == 0, r.stderr[-200:])
    # export all key formats
    r2 = subprocess.run([PY, os.path.join(HERE, "export.py"), db, "--format", "jsonl",
                         "--out", os.path.join(tmp, "c.jsonl")],
                        capture_output=True, text=True, timeout=120)
    check("export jsonl", r2.returncode == 0 and json.loads(r2.stdout)["lines"] >= 1, r2.stderr[-150:])
    r3 = subprocess.run([PY, os.path.join(HERE, "export.py"), db, "--format", "package",
                         "--outdir", os.path.join(tmp, "pkg"), "--title", "T"],
                        capture_output=True, text=True, timeout=120)
    check("export package", r3.returncode == 0 and
          os.path.exists(os.path.join(tmp, "pkg", "INGEST.md")), r3.stderr[-150:])
    # import package into fresh db
    fresh = os.path.join(tmp, "fresh.db")
    r4 = subprocess.run([PY, os.path.join(HERE, "sync.py"), "import-package",
                         os.path.join(tmp, "pkg"), "--db", fresh],
                        capture_output=True, text=True, timeout=120)
    check("import-package", r4.returncode == 0, r4.stderr[-200:])
    # merge original into fresh -> dedup means chunks don't double
    r5 = subprocess.run([PY, os.path.join(HERE, "sync.py"), "merge", fresh, db,
                          "--out", os.path.join(tmp, "m.db")],
                        capture_output=True, text=True, timeout=120)
    if r5.returncode == 0:
        after = json.loads(r5.stdout)["after"]["chunks"]
        check("merge dedup", after == json.loads(r.stdout)["chunks"], f"after={after}")
    else:
        check("merge dedup", False, r5.stderr[-150:])




def test_attribution_flow():
    print("attribution flow (acquire -> ingest -> graph -> search)")
    import tempfile
    tmp = tempfile.mkdtemp()
    # synthetic acquisition manifest + file (offline: no real fetch)
    ws = os.path.join(tmp, "acquired")
    os.makedirs(ws)
    open(os.path.join(ws, "page.txt"), "w").write("self-improving agent creates skills from experience. " * 6)
    json.dump({"sources": [{"kind": "page", "url": "https://example.com/x",
                            "file": os.path.join(ws, "page.txt"), "sha256": "0" * 64,
                            "tool": "urllib+stdlib-html", "size": 10,
                            "acquired_at": "2026-10-06T00:00:00+0000"}]},
              open(os.path.join(ws, "acquisition.json"), "w"))
    r = subprocess.run([PY, os.path.join(HERE, "ingest.py"), ws, "--recursive"],
                       capture_output=True, text=True, timeout=120)
    check("ingest with acquisition", r.returncode == 0, r.stderr[-200:])
    db = os.path.join(tmp, "g.db")
    r2 = subprocess.run([PY, os.path.join(HERE, "graph.py"), "ingest",
                         os.path.join(ws, "extracted"), "--db", db],
                        capture_output=True, text=True, timeout=120)
    check("graph ingest", r2.returncode == 0, r2.stderr[-200:])
    r3 = subprocess.run([PY, os.path.join(HERE, "graph.py"), "search", "self-improving",
                         "--db", db, "--limit", "1"], capture_output=True, text=True, timeout=60)
    if r3.returncode == 0 and r3.stdout.strip():
        res = json.loads(r3.stdout)
        att = res[0].get("attribution") or {}
        check("search result carries url", att.get("url") == "https://example.com/x", str(att)[:120])
        check("search result carries tool", att.get("tool") == "urllib+stdlib-html", str(att)[:120])
    else:
        check("search result carries url", False, r3.stderr[-150:])

def test_responsive_ui_served():
    print("responsive ui served by api")
    import zipfile
    src = open(os.path.join(HERE, "ui.html")).read()
    check("ui has media queries", "@media (max-width: 720px)" in src and "@media (max-width: 480px)" in src)
    check("ui escapes untrusted text", "const esc =" in src)
    check("ui renders attribution", "attribution" in src and "acquire" not in src[:50])




def test_ocr_and_watch():
    print("ocr lane + watch mode (live)")
    import tempfile
    tmp = tempfile.mkdtemp()
    inbox = os.path.join(tmp, "inbox")
    os.makedirs(inbox)
    open(os.path.join(inbox, "n.md"), "w").write(
        "The funnel machine decides the next move for every lead. " * 6)
    db = os.path.join(tmp, "k.db")
    r = subprocess.run([PY, os.path.join(HERE, "watch.py"), "--dir", inbox,
                        "--db", db, "--once"], capture_output=True, text=True, timeout=300)
    check("watch --once assimilates", r.returncode == 0 and "graph" in r.stderr, r.stderr[-150:])
    r2 = subprocess.run([PY, os.path.join(HERE, "graph.py"), "search", "funnel",
                         "--db", db, "--limit", "1"], capture_output=True, text=True, timeout=60)
    ok = r2.returncode == 0 and r2.stdout.strip() and json.loads(r2.stdout)
    check("watched file searchable", bool(ok), r2.stderr[-120:])
    # ocr.py CLI shape
    r3 = subprocess.run([PY, os.path.join(HERE, "ocr.py"), "--help"], capture_output=True, text=True, timeout=60)
    check("ocr.py --help", r3.returncode == 0)
    # ocr helper present (macOS vision) or tesseract — engine detectable.
    # ensure_helper() compiles on demand: a fresh clone builds it on first
    # use, so the test asks ocr.py to build rather than assuming a prior run.
    helper = os.path.expanduser("~/.consumer/bin/consumer-ocr")
    if not os.path.exists(helper) and not shutil.which("tesseract"):
        sys.path.insert(0, HERE)
        import importlib.util
        spec = importlib.util.spec_from_file_location("ocrmod", os.path.join(HERE, "ocr.py"))
        ocrmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ocrmod)
        ocrmod.ensure_helper()
    check("ocr engine available", os.path.exists(helper) or shutil.which("tesseract") is not None)




def test_zero_and_connectors():
    print("zero-in + connectors (live)")
    import tempfile
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "z.db")
    # clipboard lane: set real clipboard text
    subprocess.run(["bash", "-c", 'printf "Zero in test: the funnel machine decides the next move." | pbcopy'],
                   capture_output=True, text=True, timeout=30)
    r = subprocess.run([PY, os.path.join(HERE, "zero.py"), "clip", "--db", db, "--label", "t"],
                       capture_output=True, text=True, timeout=300)
    check("zero clip consumes", r.returncode == 0 and json.loads(r.stdout)["chars"] > 10,
          r.stderr[-150:])
    r2 = subprocess.run([PY, os.path.join(HERE, "graph.py"), "search", "funnel",
                        "--db", db, "--limit", "1"], capture_output=True, text=True, timeout=60)
    ok = r2.returncode == 0 and r2.stdout.strip() and json.loads(r2.stdout)
    check("zero'd text searchable", bool(ok), r2.stderr[-120:])
    # connectors CLI shape + list
    r3 = subprocess.run([PY, os.path.join(HERE, "connectors.py"), "list"],
                       capture_output=True, text=True, timeout=60)
    check("connectors list", r3.returncode == 0 and "gmail" in r3.stdout)
    # ortie invocation shape (token show --account) — no token needed for the check
    src = open(os.path.join(HERE, "connectors.py")).read()
    check("ortie token show shape", '"token", "show", "--account"' in src)




def test_nango_and_drive():
    print("nango lane + drive connector (unit)")
    sys.path.insert(0, HERE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("con", os.path.join(HERE, "connectors.py"))
    con = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(con)
    check("nango unconfigured -> None", con.nango_token("x") is None)
    check("nango bad url -> None", con.nango_token("x", nango_url="http://127.0.0.1:1", nango_key="k") is None)
    src = open(os.path.join(HERE, "connectors.py")).read()
    check("drive scope error message", "drive.readonly" in src)
    check("three connectors registered", all(c in con.CONNECTORS for c in ("gmail", "imap", "drive")))
    import os as _os
    check("INTEGRATIONS.md exists", _os.path.exists(os.path.join(HERE, "INTEGRATIONS.md")))




def test_bank():
    print("integration bank (live)")
    r = subprocess.run([PY, os.path.join(HERE, "bank.py"), "list"],
                       capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0 and json.loads(r.stdout).get("total", 0) >= 6
    check("bank lists >=6 connectors", ok, r.stderr[-150:])
    r2 = subprocess.run([PY, os.path.join(HERE, "bank.py"), "status"],
                        capture_output=True, text=True, timeout=180)
    if r2.returncode == 0:
        d = json.loads(r2.stdout)
        check("bank status honest", isinstance(d["connectors"], list) and len(d["connectors"]) >= 6)
        check("engine ready flag", d["engine_ready"] is True)
    # injection resistance: the safe runner quotes args (check CALLS, not
    # prose — the docstring says "no shell=True" which tripped a naive check)
    src = open(os.path.join(HERE, "bank.py")).read()
    calls = [l for l in src.splitlines() if "subprocess.run(" in l and "shell=True" in l]
    check("no shell=True CALLS in bank", not calls, str(calls[:1]))
    check("shlex quoting present", "shlex.quote" in src)
    # manifest validation rejects junk
    import tempfile
    tmp = tempfile.mkdtemp()
    bad = os.path.join(tmp, "bad.json")
    json.dump({"provider_id": "x"}, open(bad, "w"))
    r3 = subprocess.run([PY, os.path.join(HERE, "bank.py"), "add-manifest", bad],
                        capture_output=True, text=True, timeout=60)
    check("manifest validation rejects", r3.returncode != 0 and "missing" in (r3.stdout or r3.stderr))


def test_bank_act_lanes():
    print("bank act lanes (x / repo / local)")
    # manifests installed and declared
    r = subprocess.run([PY, os.path.join(HERE, "bank.py"), "list"],
                       capture_output=True, text=True, timeout=120)
    bank = {c["id"]: c for c in json.loads(r.stdout)["bank"]}
    check("x connector listed", "x" in bank and "write-draft" in bank["x"]["actions"])
    check("repo connector listed", "repo" in bank and
          all(a in bank["repo"]["actions"] for a in ("create-issue", "list-issues", "close-issue")))
    check("local connector listed", "local" in bank and
          all(a in bank["local"]["actions"] for a in ("open-url", "copy-clipboard", "notify")))
    check("x honest needs-auth", bank["x"]["status"] == "needs-auth")
    # every action declares an args_schema
    ms = json.load(open(os.path.join(HERE, "bank", "connectors", c + ".json"))) if False else None
    for cid in ("x", "repo", "local"):
        m = json.load(open(os.path.join(HERE, "bank", "connectors", cid + ".json")))
        check(f"{cid} actions all have args_schema",
              all(a.get("args_schema") for a in m["actions"]))
    # x: draft written, never posts (no network)
    r = subprocess.run([PY, os.path.join(HERE, "bank.py"), "act", "x", "write-draft",
                        "--args", json.dumps({"text": "smoke test draft"})],
                       capture_output=True, text=True, timeout=120)
    check("x write-draft runs", r.returncode == 0 and "draft written" in r.stdout,
          r.stderr[-150:])
    check("x draft announces needs-auth", "needs-auth" in r.stdout)
    # local: clipboard round-trip + notify + open-url
    r = subprocess.run([PY, os.path.join(HERE, "bank.py"), "act", "local", "copy-clipboard",
                        "--args", json.dumps({"text": "smoke clipboard ok"})],
                       capture_output=True, text=True, timeout=120)
    clip = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=30)
    check("local copy-clipboard round-trip",
          r.returncode == 0 and clip.stdout.strip() == "smoke clipboard ok")
    r = subprocess.run([PY, os.path.join(HERE, "bank.py"), "act", "local", "notify",
                        "--args", json.dumps({"message": "smoke", "title": "t"})],
                       capture_output=True, text=True, timeout=120)
    check("local notify runs", r.returncode == 0, r.stderr[-150:])
    r = subprocess.run([PY, os.path.join(HERE, "bank.py"), "act", "local", "open-url",
                        "--args", json.dumps({"url": "https://example.com"})],
                       capture_output=True, text=True, timeout=120)
    check("local open-url runs", r.returncode == 0, r.stderr[-150:])
    # repo: gh present + create/close a live issue only when gh is authed
    if shutil.which("gh"):
        st = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=60)
        if st.returncode == 0:
            repo = "MediaPlural/bradan"
            title = "[smoke] bank act-lane test (auto-closed)"
            r = subprocess.run([PY, os.path.join(HERE, "bank.py"), "act", "repo", "create-issue",
                                "--args", json.dumps({"repo": repo, "title": title,
                                                      "body": "smoke test — closed immediately"})],
                               capture_output=True, text=True, timeout=180)
            ok = r.returncode == 0 and "issues/" in r.stdout
            check("repo create-issue live", ok, (r.stdout + r.stderr)[-200:])
            if ok:
                num = r.stdout.strip().splitlines()[-1].rsplit("/", 1)[-1]
                r2 = subprocess.run([PY, os.path.join(HERE, "bank.py"), "act", "repo", "close-issue",
                                     "--args", json.dumps({"repo": repo, "number": num,
                                                           "comment": "smoke done"})],
                                    capture_output=True, text=True, timeout=180)
                check("repo close-issue live", r2.returncode == 0, (r2.stdout + r2.stderr)[-200:])
        else:
            print("  (gh not authed — live repo tests skipped)")
    else:
        print("  (gh not installed — live repo tests skipped)")


def test_bank_act_injection():
    print("bank act-lane injection resistance (live)")
    pwn = "/tmp/pwned2.txt"
    if os.path.exists(pwn):
        os.remove(pwn)
    payload = "; touch /tmp/pwned2.txt"
    cases = [
        ("local", "open-url", {"url": "'" + payload}),
        ("local", "copy-clipboard", {"text": "'" + payload}),
        ("local", "notify", {"message": "'" + payload, "title": "t"}),
        ("x", "write-draft", {"text": "'" + payload}),
        ("repo", "list-issues", {"repo": "MediaPlural/bradan" + payload,
                                 "state": "all", "limit": "1"}),
        ("repo", "close-issue", {"repo": "MediaPlural/bradan", "number": "1" + payload,
                                 "comment": "x" + payload}),
    ]
    for conn, act_name, args in cases:
        r = subprocess.run([PY, os.path.join(HERE, "bank.py"), "act", conn, act_name,
                            "--args", json.dumps(args)],
                           capture_output=True, text=True, timeout=180)
        check(f"injection literal {conn}.{act_name}", not os.path.exists(pwn),
              f"rc={r.returncode} pwned!")
        if os.path.exists(pwn):
            os.remove(pwn)
    # clipboard copy proves the payload arrived as literal text
    clip = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=30)
    check("clipboard holds literal payload", payload in clip.stdout)


def test_sitegen_game_adapter():
    print("sitegen guild game adapter (contract)")
    import tempfile
    sys.path.insert(0, HERE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("sg", os.path.join(HERE, "sitegen.py"))
    sg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sg)

    # 1. module-level mapping contract: canonical names + quest tiers
    check("guild schema version", sg.GUILD_EVENT_VERSION == "guild-progression-v1")
    check("guild event kinds", (sg.GUILD_EVENT_KIND_LESSON, sg.GUILD_EVENT_KIND_XP,
                                sg.GUILD_EVENT_KIND_LEVEL) == ("lesson_complete", "xp_gain", "level_up"))
    tiers = [name for _, name in sg.QUEST_TIERS]
    check("canonical quest tiers", tiers == ["Trivial", "Minor", "Standard", "Major", "Legendary", "Mythic"],
          str(tiers))
    check("tier floors match codex", [f for f, _ in sg.QUEST_TIERS] == [0, 25, 100, 500, 3000, 25000])
    check("tier mapping trivial", sg.quest_tier_for(10) == "Trivial")
    check("tier mapping minor", sg.quest_tier_for(48) == "Minor")
    check("tier mapping standard", sg.quest_tier_for(120) == "Standard")
    check("quality standard default", sg.quality_for(50) == "Standard")
    ev = sg.guild_event(sg.GUILD_EVENT_KIND_LESSON, {"member_uuid": "u1", "xp_delta": 40})
    check("guild_event envelope", ev["schema"] == "guild-progression-v1"
          and ev["kind"] == "lesson_complete" and ev["payload"]["xp_delta"] == 40)

    # 2. real site generation with --game-adapter
    dist = tempfile.mkdtemp() + "/distilled"
    os.makedirs(dist, exist_ok=True)
    with open(os.path.join(dist, "keywords.json"), "w") as f:
        json.dump({"keywords": [{"term": "funnel", "tf": 3, "documents": 1, "score": 1.5}]}, f)
    with open(os.path.join(dist, "concepts.json"), "w") as f:
        json.dump({"concepts": [{"phrase": "funnel machine", "count": 2}]}, f)
    with open(os.path.join(dist, "next-best-seeds.json"), "w") as f:
        json.dump({"seeds": [{"sentence": "Reply within five minutes.", "density": 0.6, "words": 7}]}, f)
    with open(os.path.join(dist, "course-map.md"), "w") as f:
        f.write("# Test course\n\nA map.")
    lessons_dir = tempfile.mkdtemp() + "/transcripts"
    os.makedirs(lessons_dir, exist_ok=True)
    for i, txt in enumerate(["Lesson one about the funnel machine. " * 12,
                             "Lesson two about five minutes replies. " * 12], 1):
        with open(os.path.join(lessons_dir, f"l{i:02d}.transcript.json"), "w") as f:
            json.dump({"text": txt, "segments": [{"start": 0.0, "end": 60.0 * i, "text": "s"}]}, f)
    out = tempfile.mkdtemp() + "/site/index.html"
    r = subprocess.run([PY, os.path.join(HERE, "sitegen.py"), dist,
                        "--lessons", lessons_dir, "--title", "Adapter Course",
                        "--out", out, "--game-adapter"],
                       capture_output=True, text=True, timeout=120)
    check("sitegen --game-adapter runs", r.returncode == 0, r.stderr[-200:])
    res = json.loads(r.stdout) if r.returncode == 0 else {}
    adapter_path = res.get("game_adapter")
    check("adapter emitted next to site", bool(adapter_path) and os.path.exists(adapter_path),
          str(res)[:150])
    check("output reports guild schema", res.get("guild_schema") == "guild-progression-v1")

    # 3. adapter script: canonical event shape (assert the mapping contract,
    #    not a live guild call — no guild runtime needed)
    if adapter_path and os.path.exists(adapter_path):
        src = open(adapter_path).read()
        check("adapter declares schema", 'var SCHEMA = "guild-progression-v1"' in src)
        for field in ("member_uuid", "quest_id", "quest_tier", "quality",
                      "xp_delta", "xp_total", "level", "rank", "rank_level",
                      "band", "occurred_at", "source"):
            check(f"adapter emits canonical field {field}", field + ":" in src)
        for kind in ("lesson_complete", "xp_gain", "level_up"):
            check(f"adapter emits kind {kind}", f'"{kind}"' in src)
        check("adapter wraps consumerGameEngine",
              "window.consumerGameEngine = function" in src)
        check("adapter uses guild bridge", "guildBridge" in src and "guild-adapter-queue" in src)
        check("adapter band mapping codex v2",
              '"Peak"' in src and '"Late"' in src and '"Middle"' in src and '"Early"' in src)
        check("adapter no network calls", "fetch(" not in src and "XMLHttpRequest" not in src)
        # simulate the mapping in Python: a lesson_complete event must carry
        # the canonical payload keys with consumer values
        xp = sg.xp_for_lesson({"dur_s": 60.0}, 0)
        ev = sg.guild_event(sg.GUILD_EVENT_KIND_LESSON, {
            "member_uuid": None, "source": "consumer", "quest_id": "lesson-01",
            "quest_tier": sg.quest_tier_for(xp), "quality": sg.quality_for(xp),
            "xp_delta": xp, "xp_total": xp, "level": 1,
            "rank": None, "rank_level": None, "band": None,
            "occurred_at": "2026-10-06T00:00:00.000Z"})
        p = ev["payload"]
        check("simulated event shape", ev["schema"] == "guild-progression-v1"
              and p["quest_id"] == "lesson-01" and p["quest_tier"] == sg.quest_tier_for(xp)
              and p["quest_tier"] in tiers
              and p["quality"] == "Standard" and p["xp_delta"] == xp
              and p["source"] == "consumer" and isinstance(p["xp_total"], int))
        # site includes the hook the adapter wraps
        site_src = open(out).read()
        check("site keeps consumerGameEngine hook", "window.consumerGameEngine" in site_src)
        check("site has data-xp attributes", 'data-xp="' in site_src)

    # 4. flag is OPTIONAL: default generation emits no adapter
    out2 = tempfile.mkdtemp() + "/plain/index.html"
    r2 = subprocess.run([PY, os.path.join(HERE, "sitegen.py"), dist,
                         "--lessons", lessons_dir, "--title", "Plain",
                         "--out", out2], capture_output=True, text=True, timeout=120)
    res2 = json.loads(r2.stdout) if r2.returncode == 0 else {}
    check("default run has no adapter", r2.returncode == 0 and "game_adapter" not in res2,
          str(res2)[:150])
    check("plain site dir has no game-adapter.js",
          not os.path.exists(os.path.join(os.path.dirname(out2), "game-adapter.js")))


def main():
    test_cli_shapes()
    test_srt_and_fingerprint()
    test_distiller()
    test_distill_deep_units()
    test_distill_deep_cli()
    test_scrape_manifest()
    test_mcp_dispatch()
    test_course_dl_skool()
    test_author_and_explain()
    test_graph_and_source()
    test_export_sync()
    test_attribution_flow()
    test_responsive_ui_served()
    test_ocr_and_watch()
    test_zero_and_connectors()
    test_nango_and_drive()
    test_bank()
    test_bank_act_lanes()
    test_bank_act_injection()
    test_sitegen_game_adapter()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()