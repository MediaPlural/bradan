#!/usr/bin/env python3
"""bradan distill — course corpus -> structured, actionable knowledge.

Input: a directory of transcripts (.txt/.transcript.json) + optional web text (.txt).

Output (in --out-dir):
  concept-graph.json    typed concept graph: nodes with provenance, edges with
                        relations (prerequisite / derived-from / contrasts /
                        part-of / example-of / supports). This is the artifact
                        an agent or knowledge base can ACT on.
  synthesis.json        summary, key takeaways, caveats/contradictions the
                        instructor flags, and "what this changes about how you
                        build/decide" notes, plus a learning path.
  course-map.md         human render of the graph + synthesis (legacy sections
                        demoted to an appendix).
  keywords.json, concepts.json, next-best-seeds.json
                        legacy shallow-pass artifacts, kept for downstream
                        consumers (sitegen, insights, HANDBOOK adapters).

Two engines, one schema:
  --llm auto (default): use a local ollama server when reachable; per-chunk
    concept/edge/takeaway extraction, then a course-level synthesis pass.
    Chunk results are cached in .distill-cache.json keyed by
    sha256(model + prompt + chunk) so re-runs are idempotent and free.
  --llm none: stdlib-only fallback (frequency/co-occurrence). Same output
    schema, relations degrade to "co-occurs", provenance says llm: null.

House laws held: stdlib only, no API keys, no content leaves the machine
unless an endpoint is explicitly pointed elsewhere via --llm-url; distill
reads transcripts and never mutates them; every source is sha256-anchored.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict

STOP = set("""a an the and or but if then else for of to in on at by with from as is are was were be been being
this that these those it its we you your our their his her they them he she i me my mine do does did done
have has had will would can could should shall may might must not no nor so than too very just about into
over under out up down off again further once here there when where why how all any both each few more most
other some such only own same s t don now what which who whom whose because while during before after above
below between through around against got get gets go goes going make makes made way also like okay yeah um""".split())

PROMPT_VERSION = "distill-chunk/v1"
SYNTH_VERSION = "distill-synthesis/v1"
DEFAULT_MODEL = os.environ.get("BRADAN_DISTILL_MODEL", "qwen3.5:4b")
DEFAULT_LLM_URL = os.environ.get("BRADAN_OLLAMA_URL", "http://localhost:11434")

EDGE_RELATIONS = ("prerequisite", "derived-from", "contrasts", "part-of",
                  "example-of", "supports")
FALLBACK_RELATION = "co-occurs"


# ---------------------------------------------------------------- transcripts

def load_sources(in_dir):
    """Return list of {name, text, dur_s, segs, words, sha256} for each transcript.
    A .txt sitting next to its own .transcript.json is a derived file — skip it."""
    files = sorted(os.listdir(in_dir))
    json_stems = {f[:-len(".transcript.json")] for f in files if f.endswith(".transcript.json")}
    srcs = []
    for fname in files:
        path = os.path.join(in_dir, fname)
        if fname.endswith(".transcript.json"):
            try:
                d = json.load(open(path, errors="replace"))
                if "segments" in d or "text" in d:
                    segs = d.get("segments", [])
                    text = d.get("text", "")
                    srcs.append({"name": fname, "text": text,
                                 "dur_s": segs[-1]["end"] if segs else None,
                                 "segs": len(segs),
                                 "words": sum(len(s.get("words", [])) for s in segs),
                                 "sha256": sha256_text(text)})
            except Exception:
                pass
        elif fname.endswith(".txt") and os.path.splitext(fname)[0] not in json_stems:
            text = open(path, errors="replace").read()
            srcs.append({"name": fname, "text": text,
                         "dur_s": None, "segs": None, "words": None,
                         "sha256": sha256_text(text)})
    if not srcs:
        sys.exit(f"FATAL: no .txt or .transcript.json files in {in_dir}")
    return srcs


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 1]


# ------------------------------------------------------- legacy shallow pass

def score_keywords(texts):
    """Frequency x length x rarity scoring across the whole corpus."""
    doc_freq = Counter()
    term_freq = Counter()
    for text in texts:
        words = re.findall(r"[a-zA-Z][a-zA-Z'-]{2,}", text.lower())
        seen = set()
        for w in words:
            if w in STOP or len(w) < 3:
                continue
            term_freq[w] += 1
            seen.add(w)
        for w in seen:
            doc_freq[w] += 1
    scored = []
    n = len(texts)
    for term, tf in term_freq.items():
        length_bonus = min(len(term), 10) / 10
        # Course doctrine: a term appearing across MANY lessons is core course
        # vocabulary (convergence = strongest signal) — so we BOOST document
        # convergence instead of penalizing it the way plain IDF does. Stop
        # words are already excluded; generic-in-any-corpus words score high
        # but the LLM layer refines downstream.
        convergence = doc_freq[term] / n if n else 0
        scored.append({"term": term, "tf": tf, "documents": doc_freq[term],
                       "score": round(tf * (0.5 + length_bonus) * (0.5 + convergence), 2)})
    scored.sort(key=lambda x: -x["score"])
    return scored


def extract_concepts(text):
    """Pattern-based concept extraction for spoken transcripts: capitalized
    multi-word phrases and definition patterns; falls back to bigrams that
    behave like terms (frequent, not stopword-led) for lowercase prose."""
    concepts = []
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b", text):
        phrase = m.group(1)
        if phrase.lower().split()[0] not in STOP:
            concepts.append(phrase)
    PRONOUNISH = {"this", "that", "it", "there", "here", "these", "those", "he", "she",
                  "they", "we", "you", "i", "what", "which", "who", "one", "thing"}
    for m in re.finditer(r"\b([a-z][a-z'-]+(?:\s+[a-z][a-z'-]+)?)\s+(?:is|are|means|makes)\s+(?:a|an|the)\b", text, re.I):
        head = m.group(1).strip().lower().split()[0]
        if head not in PRONOUNISH:
            concepts.append(m.group(1).strip())
    # spoken prose is often uncapitalized; catch frequent content bigrams
    bigrams = Counter()
    for w1, w2 in re.findall(r"\b([a-z]{3,})\s+([a-z]{3,})\b", text.lower()):
        if w1 in STOP or w2 in STOP or w1 == w2:
            continue
        bigrams[f"{w1} {w2}"] += 1
    for phrase, count in bigrams.items():
        if count >= 2:
            concepts.append(phrase)
    return concepts


# ------------------------------------------------------------- llm layer

class LLMError(Exception):
    """Model unreachable, bad HTTP, or unparseable payload after repair."""


class OllamaClient:
    """Stdlib urllib client for an ollama-compatible /api/generate endpoint.
    Local-first: the default endpoint is localhost; anything else must be
    passed explicitly via --llm-url (that is the only frontier hook)."""

    def __init__(self, url=DEFAULT_LLM_URL, model=DEFAULT_MODEL, timeout=180):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def available(self):
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=2) as r:
                tags = json.loads(r.read().decode("utf-8", "replace"))
            names = {m.get("name", "") for m in tags.get("models", [])}
            if self.model in names:
                return True, None
            return False, f"model {self.model!r} not pulled (have: {sorted(names)[:6]})"
        except Exception as e:
            return False, f"ollama unreachable at {self.url}: {e}"

    def complete(self, prompt):
        body = json.dumps({"model": self.model, "prompt": prompt, "stream": False,
                           "format": "json", "think": False,
                           "options": {"temperature": 0.2, "num_predict": 1500}}).encode()
        req = urllib.request.Request(self.url + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        last = None
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                return d.get("response", "")
            except Exception as e:
                last = e
                if attempt == 1:
                    time.sleep(1)
        raise LLMError(str(last))


def extract_json(text):
    """Tolerant JSON object extraction from a model response: find the first
    balanced {...} block and parse it."""
    start = text.find("{")
    if start < 0:
        raise LLMError("no JSON object in model response")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise LLMError("unbalanced JSON in model response")


CHUNK_PROMPT = """You are distilling one chunk of a course lesson transcript into structured knowledge for a knowledge graph.

Return ONLY a JSON object with this exact shape:
{{"concepts": [{{"name": "lowercase 1-4 word concept", "definition": "one sentence as the instructor means it"}}],
 "edges": [{{"from": "concept name", "to": "concept name", "relation": "REL"}}],
 "takeaways": ["a key takeaway as one crisp sentence"],
 "caveats": ["a contradiction, edge case, warning, or 'this breaks when...' the instructor flags"],
 "decisions": ["what this changes about how you build or decide, phrased as guidance"]}}

Rules:
- REL is one of: prerequisite (from must be understood before to), derived-from (to is derived from / built on from), contrasts (from and to are in tension or opposed), part-of, example-of, supports.
- 3 to 8 concepts. Edges only between concepts you listed. 1 to 4 items in each of takeaways, caveats, decisions; use empty lists when the chunk genuinely has none.
- Caveats are things the INSTRUCTOR flags as exceptions or failures, not generic warnings.
- No commentary, no markdown fences. JSON only.

Source: {source}
Transcript chunk:
\"\"\"
{text}
\"\"\"
"""

SYNTH_PROMPT = """You are consolidating chunk-level distillations of a full course into a course-level synthesis.

Here are the concepts, typed edges, and takeaways extracted chunk by chunk (JSON):
{payload}

Return ONLY a JSON object with this exact shape:
{{"summary": "3-5 sentences: what this course teaches and who it changes",
 "learning_path": ["concept names from the graph, ordered so prerequisites come first"],
 "core_caveats": ["the 3-6 most important contradictions / edge cases / failure modes across the whole course"],
 "decision_notes": ["the 3-6 most consequential 'what this changes about how you build or decide' notes"]}}

Rules: use concept names exactly as given; no commentary; JSON only.
"""


def cache_key(model, version, payload):
    return hashlib.sha256(f"{model}\n{version}\n{payload}".encode()).hexdigest()


def load_cache(out_dir):
    path = os.path.join(out_dir, ".distill-cache.json")
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            pass
    return {"chunks": {}, "synthesis": {}}


def save_cache(out_dir, cache):
    tmp = os.path.join(out_dir, ".distill-cache.json.tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, os.path.join(out_dir, ".distill-cache.json"))


def chunk_text(text, max_words=900, overlap=80):
    """Word-bounded chunks with a small overlap so cross-boundary concepts
    survive. Returns list of chunk strings."""
    words = text.split()
    if len(words) <= max_words:
        return [text.strip()] if text.strip() else []
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + max_words])
        # prefer ending on sentence punctuation near the bound
        cut = chunk.rfind(". ")
        if cut > max_words * 6 and i + max_words < len(words):
            chunk = chunk[:cut + 1]
        chunks.append(chunk.strip())
        i += max(1, len(chunk.split()) - overlap)
    return chunks


def distill_chunk_llm(client, chunk, source_name, cache, use_cache=True):
    """One chunk -> typed extraction. Idempotent: identical (model, prompt
    version, chunk text) hits the cache instead of the model."""
    key = cache_key(client.model, PROMPT_VERSION, source_name + "\n" + chunk)
    if use_cache and key in cache["chunks"]:
        ext = dict(cache["chunks"][key])
        ext["_cached"] = True
        return ext
    prompt = CHUNK_PROMPT.format(source=source_name, text=chunk)
    raw = client.complete(prompt)
    ext = normalize_extraction(extract_json(raw), chunk)
    cache["chunks"][key] = ext
    return ext


def normalize_extraction(d, chunk):
    """Clamp a model extraction to the declared shape; drop malformed items
    rather than failing the whole chunk."""
    out = {"concepts": [], "edges": [], "takeaways": [], "caveats": [], "decisions": []}
    for c in d.get("concepts", [])[:12]:
        if isinstance(c, dict) and c.get("name"):
            name = canon(c["name"])
            if name and name.split()[0] not in STOP:
                out["concepts"].append({"name": name,
                                        "definition": str(c.get("definition", ""))[:300]})
    known = {c["name"] for c in out["concepts"]}
    for e in d.get("edges", [])[:16]:
        if isinstance(e, dict) and e.get("from") and e.get("to"):
            a, b = canon(e["from"]), canon(e["to"])
            rel = str(e.get("relation", "")).strip().lower()
            if a in known and b in known and a != b:
                if rel not in EDGE_RELATIONS:
                    rel = "supports"
                out["edges"].append({"from": a, "to": b, "relation": rel})
    for key in ("takeaways", "caveats", "decisions"):
        for item in d.get(key, [])[:6]:
            if isinstance(item, str) and len(item.strip()) > 8:
                out[key].append(item.strip()[:400])
    out["chunk_sha256"] = sha256_text(chunk)
    return out


def canon(name):
    """Canonical concept key: lowercase, whitespace-collapsed, punctuation-stripped."""
    n = re.sub(r"[^a-z0-9 '\-]", " ", str(name).lower())
    return re.sub(r"\s+", " ", n).strip()


# ------------------------------------------------------------- graph build

def merge_extractions(extrs):
    """Chunk extractions -> one concept graph. Nodes carry provenance
    (source files + chunk sha + definition); edges dedupe on
    (from, to, relation) and accumulate weight."""
    nodes = {}
    edges = {}
    for ext in extrs:
        for c in ext.get("concepts", []):
            name = canon(c["name"])
            if not name:
                continue
            n = nodes.setdefault(name, {"id": slug(name), "name": name,
                                        "count": 0, "definition": c.get("definition", ""),
                                        "sources": [], "chunks": []})
            n["count"] += 1
            if not n["definition"] and c.get("definition"):
                n["definition"] = c["definition"]
            for prov_key, prov_list in (("_source", n["sources"]), ("chunk_sha256", n["chunks"])):
                v = ext.get(prov_key)
                if v and v not in prov_list:
                    prov_list.append(v)
        for e in ext.get("edges", []):
            a_, b_ = canon(e["from"]), canon(e["to"])
            k = (a_, b_, e["relation"])
            edge = edges.setdefault(k, {"from": a_, "to": b_,
                                        "relation": e["relation"], "weight": 0,
                                        "sources": []})
            edge["weight"] += 1
            s = ext.get("_source")
            if s and s not in edge["sources"]:
                edge["sources"].append(s)
    return {"nodes": sorted(nodes.values(), key=lambda n: -n["count"]),
            "edges": sorted(edges.values(), key=lambda e: (-e["weight"], e["from"]))}


def slug(name):
    return "c_" + hashlib.sha256(name.encode()).hexdigest()[:10]


def build_fallback_graph(srcs):
    """Stdlib-only graph: concepts from the pattern extractor; edges from
    sentence-level co-occurrence (relation 'co-occurs'). Same schema as the
    LLM graph so downstream consumers never branch."""
    extrs = []
    for s in srcs:
        counts = Counter(canon(p) for p in extract_concepts(s["text"]))
        kept = {c for c, n in counts.most_common(40) if c and len(c) > 2}
        ext = {"concepts": [{"name": c, "definition": ""} for c, _ in counts.most_common(40)
                            if c and len(c) > 2],
               "edges": [], "takeaways": [], "caveats": [], "decisions": [],
               "chunk_sha256": s["sha256"], "_source": s["name"]}
        # sentence-level co-occurrence among kept concepts (substring with
        # word boundaries on the canon'd sentence — cheap and deterministic)
        co = Counter()
        for sent in sentences(s["text"])[:3000]:
            cs = " " + canon(sent) + " "
            present = sorted(p for p in kept if f" {p} " in cs)
            for i in range(len(present)):
                for j in range(i + 1, len(present)):
                    co[(present[i], present[j])] += 1
        for (a_, b_), w in co.most_common(60):
            if w >= 2:
                ext["edges"].append({"from": a_, "to": b_, "relation": FALLBACK_RELATION})
        extrs.append(ext)
    return extrs


def learning_path(graph):
    """Topological walk over prerequisite edges (a -> b means learn a first),
    remaining concepts appended by frequency."""
    prereq = defaultdict(set)
    indeg = defaultdict(int)
    names = {n["name"] for n in graph["nodes"]}
    order_hint = {n["name"]: i for i, n in enumerate(graph["nodes"])}
    for e in graph["edges"]:
        if e["relation"] == "prerequisite":
            if e["to"] not in prereq[e["from"]]:
                prereq[e["from"]].add(e["to"])
                indeg[e["to"]] += 1
    ready = sorted([n for n in names if indeg[n] == 0], key=lambda n: order_hint[n])
    path, seen = [], set()
    while ready:
        n = ready.pop(0)
        if n in seen:
            continue
        seen.add(n)
        path.append(n)
        for m in sorted(prereq[n], key=lambda x: order_hint[x]):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    for n in graph["nodes"]:
        if n["name"] not in seen:
            path.append(n["name"])
    return path


def synthesize_course_llm(client, graph, extrs, cache, use_cache=True):
    """Course-level pass over the merged graph -> summary, learning path,
    core caveats, decision notes. Cached like chunks."""
    payload = json.dumps({
        "concepts": [{"name": n["name"], "definition": n["definition"], "mentions": n["count"]}
                     for n in graph["nodes"][:40]],
        "edges": graph["edges"][:60],
        "takeaways": [t for e in extrs for t in e.get("takeaways", [])][:40],
        "caveats": [t for e in extrs for t in e.get("caveats", [])][:30],
        "decisions": [t for e in extrs for t in e.get("decisions", [])][:30]})
    key = cache_key(client.model, SYNTH_VERSION, payload)
    if use_cache and key in cache["synthesis"]:
        out = dict(cache["synthesis"][key])
        out["_cached"] = True
        return out
    raw = client.complete(SYNTH_PROMPT.format(payload=payload))
    d = extract_json(raw)
    out = {"summary": str(d.get("summary", ""))[:1200],
           "learning_path": [canon(x) for x in d.get("learning_path", []) if isinstance(x, str)][:40],
           "core_caveats": [str(x)[:400] for x in d.get("core_caveats", []) if isinstance(x, str)][:8],
           "decision_notes": [str(x)[:400] for x in d.get("decision_notes", []) if isinstance(x, str)][:8]}
    cache["synthesis"][key] = out
    return out


def fallback_synthesis(graph, seeds):
    """Stdlib synthesis: the honest cheap version. Summary is a provenance
    header; takeaways are the high-density seed sentences; path is topological."""
    return {"summary": "stdlib fallback distillation (no local model reachable). "
                       "Nodes are pattern-extracted concepts; edges are sentence "
                       "co-occurrence, not instructor intent.",
            "learning_path": learning_path(graph),
            "core_caveats": [],
            "decision_notes": [],
            "takeaways": [sd["sentence"] for sd in seeds[:12]]}


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Course corpus -> concept graph + synthesis + map")
    ap.add_argument("in_dir", help="directory of transcripts (.txt / .transcript.json)")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--top-k", type=int, default=200)
    ap.add_argument("--llm", choices=["auto", "ollama", "none"], default="auto",
                    help="auto: ollama if reachable, else stdlib fallback (default)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--llm-url", default=DEFAULT_LLM_URL,
                    help="ollama-compatible endpoint; non-localhost is opt-in (content leaves the machine)")
    ap.add_argument("--chunk-words", type=int, default=900)
    ap.add_argument("--refresh", action="store_true", help="ignore the chunk cache")
    a = ap.parse_args()

    srcs = load_sources(a.in_dir)
    out_dir = a.out_dir or os.path.join(a.in_dir, "distilled")
    os.makedirs(out_dir, exist_ok=True)

    full_texts = [s["text"] for s in srcs]

    # ---- legacy shallow pass (kept: downstream consumers read these files)
    keywords = score_keywords(full_texts)[:a.top_k]
    all_text = "\n".join(full_texts)
    concept_counter = Counter(extract_concepts(all_text))
    concepts = [{"phrase": p, "count": c} for p, c in concept_counter.most_common(150)]
    kw_terms = {k["term"] for k in keywords[:60]}
    seeds = []
    for s in sentences(all_text)[:2000]:
        words = re.findall(r"[a-z']+", s.lower())
        if not words:
            continue
        density = sum(1 for w in words if w in kw_terms) / len(words)
        if density >= 0.20 and len(words) >= 6:
            seeds.append({"sentence": s[:300], "density": round(density, 2), "words": len(words)})
    seeds.sort(key=lambda x: -x["density"])
    seeds = seeds[:40]

    with open(os.path.join(out_dir, "keywords.json"), "w") as f:
        json.dump({"keywords": keywords}, f, indent=2)
    with open(os.path.join(out_dir, "concepts.json"), "w") as f:
        json.dump({"concepts": concepts}, f, indent=2)
    with open(os.path.join(out_dir, "next-best-seeds.json"), "w") as f:
        json.dump({"seeds": seeds}, f, indent=2)

    # ---- deep pass: engine selection
    llm_info = None
    client = None
    if a.llm != "none":
        client = OllamaClient(url=a.llm_url, model=a.model)
        ok, why = client.available()
        if ok:
            llm_info = {"mode": "ollama", "url": a.llm_url, "model": a.model,
                        "prompt_version": PROMPT_VERSION, "synth_version": SYNTH_VERSION}
        elif a.llm == "ollama":
            sys.exit(f"FATAL: --llm ollama requested but not available: {why}")
        else:
            print(f"[distill] ollama unavailable ({why}); using stdlib fallback",
                  file=sys.stderr)

    cache = load_cache(out_dir)
    use_cache = not a.refresh
    failed_chunks = []

    if llm_info:
        extrs = []
        chunks_total = 0
        for s in srcs:
            for i, chunk in enumerate(chunk_text(s["text"], max_words=a.chunk_words)):
                chunks_total += 1
                try:
                    ext = distill_chunk_llm(client, chunk, s["name"], cache, use_cache)
                except LLMError as e:
                    failed_chunks.append({"source": s["name"], "chunk": i, "error": str(e)[:120]})
                    # degraded stdlib slice for this chunk so nothing is dropped
                    cons = [canon(p) for p in extract_concepts(chunk)]
                    ext = {"concepts": [{"name": c, "definition": ""} for c, _ in Counter(cons).most_common(12) if c],
                           "edges": [], "takeaways": [], "caveats": [], "decisions": [],
                           "chunk_sha256": sha256_text(chunk)}
                ext["_source"] = s["name"]
                extrs.append(ext)
        graph = merge_extractions(extrs)
        try:
            synth = synthesize_course_llm(client, graph, extrs, cache, use_cache)
        except LLMError as e:
            print(f"[distill] synthesis pass failed ({e}); using fallback synthesis", file=sys.stderr)
            synth = fallback_synthesis(graph, seeds)
        chunk_takeaways = [t for e in extrs for t in e.get("takeaways", [])][:20]
        if chunk_takeaways:
            synth["takeaways"] = chunk_takeaways
        path = synth.get("learning_path") or learning_path(graph)
    else:
        extrs = build_fallback_graph(srcs)
        graph = merge_extractions(extrs)
        synth = fallback_synthesis(graph, seeds)
        path = synth["learning_path"]

    save_cache(out_dir, cache)

    llm_calls = None  # informational only; cache hits make re-runs free whether or not we count
    provenance = {
        "sources": [{"name": s["name"], "sha256": s["sha256"],
                     "words": len(s["text"].split())} for s in srcs],
        "llm": llm_info,
        "failed_chunks": failed_chunks,
        "generated_by": "bradan/distill.py",
    }
    graph_doc = {"schema": "concept-graph/bradan-v1",
                 "generated_at": int(time.time()),
                 "provenance": provenance,
                 "nodes": graph["nodes"],
                 "edges": graph["edges"]}
    synthesis_doc = {"schema": "synthesis/bradan-v1",
                     "generated_at": int(time.time()),
                     "provenance": provenance,
                     "summary": synth.get("summary", ""),
                     "learning_path": path,
                     "takeaways": synth.get("takeaways", []),
                     "core_caveats": synth.get("core_caveats", []),
                     "decision_notes": synth.get("decision_notes", [])}
    with open(os.path.join(out_dir, "concept-graph.json"), "w") as f:
        json.dump(graph_doc, f, indent=2)
    with open(os.path.join(out_dir, "synthesis.json"), "w") as f:
        json.dump(synthesis_doc, f, indent=2)

    # ---- human render
    mode = ("ollama " + llm_info["model"]) if llm_info else "stdlib fallback"
    title = os.path.basename(os.path.abspath(a.in_dir))
    if title in ("transcripts", ".", ""):
        title = os.path.basename(os.path.dirname(os.path.abspath(a.in_dir)))
    total_words = sum(len(s["text"].split()) for s in srcs)
    dur_total = sum(s["dur_s"] or 0 for s in srcs)
    lines = [f"# Course Map: {title}", "",
             f"- Sources: {len(srcs)}", f"- Total words: {total_words}",
             f"- Total audio duration: {round(dur_total/60, 1)} min",
             f"- Distillation: {mode}", ""]
    if synth.get("summary"):
        lines += ["## Summary", "", synth["summary"], ""]
    lines += ["## Learning path (prerequisites first)", ""]
    defs = {n["name"]: n["definition"] for n in graph["nodes"]}
    for i, name in enumerate(path[:25], 1):
        d = f" — {defs[name]}" if defs.get(name) else ""
        lines.append(f"{i}. **{name}**{d}")
    by_rel = defaultdict(list)
    for e in graph["edges"]:
        by_rel[e["relation"]].append(e)
    lines += ["", f"## Concept graph — {len(graph['nodes'])} concepts, {len(graph['edges'])} edges", ""]
    for rel in EDGE_RELATIONS + (FALLBACK_RELATION,):
        if not by_rel.get(rel):
            continue
        lines.append(f"### {rel}")
        lines.append("")
        for e in by_rel[rel][:25]:
            lines.append(f"- **{e['from']}** → **{e['to']}** (x{e['weight']})")
        lines.append("")
    if synth.get("takeaways"):
        lines += ["## Key takeaways", ""]
        lines += [f"{i}. {t}" for i, t in enumerate(synth["takeaways"][:15], 1)] + [""]
    if synth.get("core_caveats"):
        lines += ["## Caveats & contradictions the instructor flags", ""]
        lines += [f"- {c}" for c in synth["core_caveats"]] + [""]
    if synth.get("decision_notes"):
        lines += ["## What this changes about how you build/decide", ""]
        lines += [f"- {d}" for d in synth["decision_notes"]] + [""]
    lines += ["## Sources", ""]
    for s in srcs:
        d = f" ({round(s['dur_s']/60,1)} min)" if s.get("dur_s") else ""
        lines.append(f"- `{s['name']}`{d} — sha256 `{s['sha256'][:12]}…`")
    lines += ["", "---", "", "## Appendix: top 30 keywords", ""]
    for k in keywords[:30]:
        lines.append(f"- **{k['term']}** — tf {k['tf']}, in {k['documents']}/{len(srcs)} sources, score {k['score']}")
    lines += ["", "## Appendix: high-signal sentences", ""]
    for sd in seeds[:15]:
        lines.append(f"- density {sd['density']}: \"{sd['sentence'][:140]}\"")
    with open(os.path.join(out_dir, "course-map.md"), "w") as f:
        f.write("\n".join(lines) + "\n")

    print(json.dumps({"ok": True, "sources": len(srcs), "keywords": len(keywords),
                      "concepts": len(concepts), "seeds": len(seeds),
                      "graph_nodes": len(graph["nodes"]), "graph_edges": len(graph["edges"]),
                      "engine": mode, "failed_chunks": len(failed_chunks),
                      "cached": llm_info is not None and use_cache,
                      "out_dir": out_dir}, indent=2))


if __name__ == "__main__":
    main()
