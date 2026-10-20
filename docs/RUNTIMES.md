# RUNTIMES.md — one engine, every runtime

The consumer is CLI-first: the engine is its commands. Skills, rules, and MCP
servers are thin adapters that tell each runtime how to invoke the same CLI.

| Runtime         | Adapter shape                                       | Install target                                           |
| --------------- | --------------------------------------------------- | -------------------------------------------------------- |
| Hermes          | AgentSkills SKILL.md                                | `~/.hermes/skills/consumer`                              |
| Claude Code     | AgentSkills SKILL.md (+ `/consumer` slash command)  | `~/.claude/skills/consumer`                              |
| OpenClaw        | AgentSkills SKILL.md (user-invocable slash command) | `~/.openclaw/skills/consumer`                            |
| Cursor          | SKILL.md (project or user store) + `.mdc` rule      | `.cursor/skills/consumer` + `.cursor/rules/consumer.mdc` |
| VS Code/Copilot | MCP server + rule                                   | `.vscode/mcp.json` + user settings                       |
| Any MCP client  | stdio server `mcp-server.py` (5 tools)              | client's MCP config                                      |

## Install / refresh all runtimes

```bash
bash install-everywhere.sh            # all runtimes, detected presence
bash install-everywhere.sh --vscode-code "Code"   # VS Code variant on macOS
```

The installer detects which runtimes are present and installs/refreshes
adapters for exactly those, writing a per-run report. OpenClaw is not yet
installed on this machine; when it lands, `install-everywhere.sh` picks it up.

## Remote/agent invocation

Any remote agent can use the engine via:

1. The MCP stdio server: `python3 mcp-server.py` (tools: source, transcribe,
   scrape, distill, graph_query; graph db via BRADAN_GRAPH_DB env)
2. The HTTP API: `api.py` (8 routes; /search /semantic /hybrid /filter
   /insight /maths /export /status + POST /ingest; env BRADAN_GRAPH_DB selects db)
3. Direct CLI over ssh

## Laws (apply to every runtime adapter)

- Credentials: segmented store `~/.consumer/creds/<platform>/` (broker
  pattern) or keychain; never in code, argv, or logs.
- Attribution inbuilt: url + credential-identity + timestamp everywhere.
- SAFE runner for bank actions: shlex-quote, argv, no shell=True.
- No DRM/paywall breaking; owned/licensed material only.
