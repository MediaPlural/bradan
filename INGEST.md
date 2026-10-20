# bradan — Course Consumer & Assimilator

A big concept from a course — configured to work with your agent stack.

> Full docs: [README.md](README.md) — pipeline rationale, pins, design law.

## AGENTS.md-kin instructions for agents working in THIS repo

- The core is stdlib-only. New core deps require a PR that justifies them against the adopt-don't-rebuild law (zero API keys, zero cloud calls).
- `transcribe.py` runs the STT engine in a scrubbed child env: PYTHONPATH/VIRTUAL_ENV/CONDA\* are stripped, ffmpeg's dir is placed first on PATH. Do not "simplify" the env scrub — host kernels leak site-packages into subprocesses and break the venv.
- Pinned provenance: `mlx-whisper==0.4.3`, model `mlx-community/whisper-large-v3-turbo` @ revision `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb`. Output-affecting changes (model, revision, options) bump `TRANSCRIPT_PIPELINE_VERSION` in transcribe.py.
- Every stage writes a provenance record: scrape → `acquisition.json` (url, sha256, tool, timestamp); transcribe → `<stem>.sha256`; distill reads transcripts, never mutates them, and sha256-anchors every source + LLM chunk in `concept-graph.json` provenance.
- Distill deep pass: local ollama when reachable (`--llm auto` default; `--model`, `--llm-url` to override — a non-localhost URL is the explicit opt-in for content leaving the machine). Chunk extractions are cached in `.distill-cache.json` keyed by sha256(model + prompt version + chunk), so re-runs are idempotent and free; `--refresh` busts the cache. `--llm none` forces the stdlib fallback, which emits the same `concept-graph.json` schema with `co-occurs` edges.
- Tests: `python3 tests/test_smoke.py` — fast, offline, no model download. Run before every PR. CI (GitHub Actions) runs the same suite.
- Commit style: imperative subject, body explains _why_ (what wall it removes). PRs follow the same law.
- When adding a capability, ask: does this belong in core (works for anyone with a folder of course files) or in an adapter (deployment-specific wiring)? Adapters go to HANDBOOK.md.

## Pipeline map (machine-readable)

```yaml
pipeline:
  acquire: { tool: scrape.py, inputs: [url], outputs: [media/page file, acquisition.json] }
  transcribe:
    { tool: transcribe.py, inputs: [media file], outputs: [transcript.json, txt, srt, sha256] }
  distill:
    {
      tool: distill.py,
      inputs: [transcript dir],
      outputs: [concept-graph.json, synthesis.json, course-map.md,
                keywords.json, concepts.json, next-best-seeds.json],
      provenance: [source sha256, chunk sha256, model + prompt version],
    }
  assimilate:
    { tool: HANDBOOK.md adapters, inputs: [distilled dir], outputs: [brain page / render script] }
```

## Quick start for agents

```bash
bash install.sh && export BRADAN_VENV_PYTHON=~/.hermes/venvs/bradan/bin/python
python3 scrape.py "URL" --out-dir ./course/media --audio-only
python3 transcribe.py ./course/media/FILE --out-dir ./course/transcripts
python3 distill.py ./course/transcripts --out-dir ./course/distilled
```

Or the wrapper: `python3 consumer.py "URL-or-dir" --tag my-course`

Or via MCP: run `mcp-server.py` and call tools `transcribe`, `scrape`, `distill`, `status`.
