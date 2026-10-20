#!/usr/bin/env python3
"""consumer MCP server — agents drive the pipeline via MCP (stdio JSON-RPC).

Tools:
  transcribe {source, out_dir?}    → transcript JSON/TXT/SRT/sha256
  scrape     {url, out_dir?, audio_only?} → media/page + acquisition.json
  distill    {in_dir, out_dir?, top_k?}    → keywords/concepts/seeds/course-map

Run: python3 mcp-server.py
Protocol: MCP stdio — JSON-RPC 2.0 over line-delimited messages.
No external deps: implements initialize + tools/list + tools/call.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VENV = os.environ.get("BRADAN_VENV_PYTHON",
                              os.path.expanduser("~/.hermes/venvs/bradan/bin/python"))

TOOLS = [
    {
        "name": "transcribe",
        "description": "Transcribe a local video/audio file to a word-timestamped transcript "
                       "(JSON + TXT + SRT + sha256 provenance).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "path to video/audio file"},
                "out_dir": {"type": "string", "description": "output directory (default: alongside source)"},
                "language": {"type": "string", "description": "ISO code or 'auto'"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "scrape",
        "description": "Acquire course material: video platform link → media file (yt-dlp); "
                       "web page → extracted text. Writes acquisition.json provenance manifest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "out_dir": {"type": "string", "description": "where media lands (default ./acquired)"},
                "audio_only": {"type": "boolean", "description": "video → audio only (smaller; enough for transcript)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "distill",
        "inputSchema": {
            "type": "object",
            "description": "Distill a directory of transcripts into a typed concept graph, "
                            "course synthesis (takeaways, caveats, decision notes), and a "
                            "course map. Uses local ollama when reachable; stdlib fallback "
                            "otherwise.",
            "properties": {
                "in_dir": {"type": "string", "description": "directory of transcripts"},
                "out_dir": {"type": "string"},
                "top_k": {"type": "integer", "description": "keywords to keep (default 200)"},
                "llm": {"type": "string", "enum": ["auto", "ollama", "none"],
                        "description": "engine (default auto)"},
                "model": {"type": "string", "description": "ollama model (default qwen3.5:4b)"},
            },
            "secrets": [],
            "required": ["in_dir"],
        },
        "description": "Distill a directory of transcripts into a typed concept graph, "
                       "course synthesis, and a course map.",
    },
    {
        "name": "source",
        "description": "Point the consumer at ANY source (website, zip, gdrive link, local dir, course URL) — resolves, acquires, ingests, and builds the vectorized graph (--full).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "URL / path / zip / dir"},
                "workspace": {"type": "string", "description": "workspace dir (default ./consumer-workspace)"},
                "db": {"type": "string", "description": "graph db path (default ./consumer.graph.db)"},
                "full": {"type": "boolean", "description": "chain ingest + graph automatically (default true)"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "graph_query",
        "description": "Query the assimilated knowledge graph: search (FTS), semantic (cosine), hybrid (fused), filter, insight (cross-source bridges — discovery), maths (corpus stats).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["search", "semantic", "hybrid", "filter", "insight", "maths"]},
                "query": {"type": "string", "description": "query text (search/semantic/hybrid)"},
                "source": {"type": "string", "description": "filter: source name contains"},
                "kind": {"type": "string", "description": "filter: source kind"},
                "limit": {"type": "integer"},
                "db": {"type": "string", "description": "graph db path"},
            },
            "required": ["mode"],
        },
    },
]


def call_tool(name, args):
    if name == "transcribe":
        cmd = ["python3", os.path.join(HERE, "transcribe.py"), args["source"]]
        if args.get("out_dir"):
            cmd += ["--out-dir", args["out_dir"]]
        if args.get("language"):
            cmd += ["--language", args["language"]]
    elif name == "scrape":
        cmd = ["python3", os.path.join(HERE, "scrape.py"), args["url"]]
        if args.get("out_dir"):
            cmd += ["--out-dir", args["out_dir"]]
        if args.get("audio_only"):
            cmd += ["--audio-only"]
    elif name == "distill":
        cmd = ["python3", os.path.join(HERE, "distill.py"), args["in_dir"]]
        if args.get("out_dir"):
            cmd += ["--out-dir", args["out_dir"]]
        if args.get("top_k"):
            cmd += ["--top-k", str(args["top_k"])]
        if args.get("llm"):
            cmd += ["--llm", args["llm"]]
        if args.get("model"):
            cmd += ["--model", args["model"]]
    elif name == "source":
        cmd = ["python3", os.path.join(HERE, "source.py"), args["source"]]
        if args.get("workspace"):
            cmd += ["--workspace", args["workspace"]]
        if args.get("db"):
            cmd += ["--db", args["db"]]
        if args.get("full", True):
            cmd += ["--full"]
    elif name == "graph_query":
        mode = args["mode"]
        cmd = ["python3", os.path.join(HERE, "graph.py"), mode]
        if mode in ("search", "semantic", "hybrid"):
            cmd += [args.get("query", "")]
        if mode == "filter":
            if args.get("source"):
                cmd += ["--source", args["source"]]
            if args.get("kind"):
                cmd += ["--kind", args["kind"]]
        if args.get("limit"):
            cmd += ["--limit", str(args["limit"])]
        if args.get("db"):
            cmd += ["--db", args["db"]]
    else:
        return {"isError": True, "content": [{"type": "text", "text": f"unknown tool: {name}"}]}
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    text = (proc.stdout or "") + ((proc.stderr or "") if proc.returncode != 0 else "")
    return {"content": [{"type": "text", "text": text[-8000:]}],
            "isError": proc.returncode != 0}


def rpc(result_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": result_id, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            rpc(mid, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "consumer", "version": "0.1.0"}})
        elif method == "notifications/initialized" or method is None:
            continue
        elif method == "tools/list":
            rpc(mid, {"tools": TOOLS})
        elif method == "tools/call":
            rpc(mid, call_tool(msg["params"]["name"], msg["params"].get("arguments", {})))
        else:
            if mid is not None:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                             "error": {"code": -32601, "message": f"unknown method {method}"}}) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    main()