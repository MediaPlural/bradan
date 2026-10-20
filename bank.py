#!/usr/bin/env python3
"""consumer bank — the integration bank. Our answer to IFTTT/Zapier/Composio.

A BANK, not a walled garden: every connector is a declarative manifest
(bank/connectors/*.json) + optional Python module. The community adds
connectors by adding manifests — the "tool others help us update" law.

Custody law carries over: local-first auth (ortie/keychain/creds store),
Nango opt-in for the long tail, cloud SaaS never holds your tokens.

Each connector declares:
  capabilities   read (consume archives) / act (do things) / subscribe (events)
  auth_method    none | token | ortie | nango | keychain
  consume        the command that pulls an archive into the graph
  actions        act-lane tools (each a shell template + arg schema)

Usage:
  python3 bank.py list                    # every connector + status
  python3 bank.py status                  # health: auth present? engine ready?
  python3 bank.py consume CONNECTOR ...    # run a connector's consume lane
  python3 bank.py act CONNECTOR ACTION --args ' {...}'    # act lane
  python3 bank.py add-manifest PATH      # validate + install a community manifest
"""
import argparse
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BANK_DIR = os.path.join(HERE, "bank")
CONNECTOR_DIR = os.path.join(BANK_DIR, "connectors")


def run_template(cmd_template, args, timeout, extra=()):
    """SAFE runner for manifest command templates — no shell=True anywhere.
    Values are shlex-quoted before substitution, then the whole command is
    shlex.split into argv. An arg containing `; rm -rf` stays a literal
    string: no metacharacter can become syntax. (A naive `cmd % args` +
    shell=True here would have been command injection by design.)"""
    cmd = cmd_template.replace("{here}", shlex.quote(HERE))
    for k, v in (args or {}).items():
        cmd = cmd.replace("{" + k + "}", shlex.quote(str(v)))
    leftover = re.findall(r"\{[a-z_]+\}", cmd)
    if leftover:
        sys.exit(f"FATAL: missing args for {leftover}")
    if extra:
        cmd += " " + " ".join(shlex.quote(x) for x in extra)
    argv = shlex.split(cmd)
    r = subprocess.run(argv, timeout=timeout)
    return r.returncode


def load_manifests():
    manifests = {}
    for p in sorted(glob.glob(os.path.join(CONNECTOR_DIR, "*.json"))):
        try:
            d = json.load(open(p))
            manifests[d["provider_id"]] = d
        except Exception as e:
            print(f"[bank] bad manifest {p}: {e}", file=sys.stderr)
    return manifests


def validate_manifest(d):
    """The manifest contract. Returns list of errors (empty = valid)."""
    errs = []
    for k in ("provider_id", "display_name", "auth_method", "capabilities", "status"):
        if k not in d:
            errs.append(f"missing: {k}")
    if "auth_method" in d and d["auth_method"] not in ("none", "token", "ortie", "nango", "keychain", "oauth"):
        errs.append(f"bad auth_method: {d['auth_method']}")
    if "capabilities" in d and not set(d["capabilities"]) <= {"read", "act", "subscribe"}:
        errs.append(f"bad capabilities: {d['capabilities']}")
    if "read" in d.get("capabilities", []) and "consume" not in d:
        errs.append("read connector needs a consume block")
    if "act" in d.get("capabilities", []) and "actions" not in d:
        errs.append("act connector needs an actions block")
    return errs


def auth_state(manifest):
    """Honest per-connector auth check — never prints secrets."""
    m = manifest["auth_method"]
    if m == "none":
        return True, "no auth needed"
    if m == "token":
        env = manifest.get("token_env", "")
        ok = bool(os.environ.get(env))
        return ok, f"env {env} {'set' if ok else 'MISSING'}"
    if m == "ortie":
        acct = manifest.get("ortie_account", "")
        r = subprocess.run(["ortie", "token", "show", "--account", acct],
                           capture_output=True, text=True, timeout=60)
        ok = r.returncode == 0 and (r.stdout or "").strip()
        return bool(ok), f"ortie:{acct} {'ready' if ok else 'no token'}"
    if m == "nango":
        ok = bool(os.environ.get("BRADAN_NANGO_URL") and os.environ.get("BRADAN_NANGO_KEY"))
        return ok, "nango env " + ("set" if ok else "MISSING")
    if m == "keychain":
        service = manifest.get("keychain_service", "")
        r = subprocess.run(["security", "find-generic-password", "-s", service],
                           capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
        return ok, f"keychain {service} {'present' if ok else 'absent'}"
    if m == "oauth":
        return False, "oauth: run connectors.py auth flow"
    return False, "unknown auth"


def consume(connector, extra_args, db):
    """Run a connector's consume lane (the command is a template: {db} {args})."""
    m = load_manifests().get(connector)
    if not m:
        sys.exit(f"FATAL: no connector '{connector}' — bank.py list")
    cmd_template = m.get("consume", {}).get("command")
    if not cmd_template:
        sys.exit(f"FATAL: {connector} has no consume lane")
    ok, auth_msg = auth_state(m)
    if not ok:
        sys.exit(f"FATAL: {connector} not authorized — {auth_msg}")
    print(f"[bank] {connector}: consume", file=sys.stderr)
    rc = run_template(cmd_template, {"db": db or "consumer.graph.db"}, 7200, extra_args)
    sys.exit(rc)


def act(connector, action, args_json, db):
    """Act lane: run one declared action. Actions are shell templates with
    {args} substitution; declared arg schemas live in the manifest."""
    m = load_manifests().get(connector)
    if not m:
        sys.exit(f"FATAL: no connector '{connector}'")
    actions = {a["id"]: a for a in m.get("actions", [])}
    if action not in actions:
        sys.exit(f"FATAL: {connector} has no action '{action}' "
                 f"(has: {', '.join(actions) or 'none'})")
    a = actions[action]
    ok, auth_msg = auth_state(m)
    if not ok:
        sys.exit(f"FATAL: {connector} not authorized — {auth_msg}")
    args = json.loads(args_json) if args_json else {}
    print(f"[bank] {connector}.{action}: act", file=sys.stderr)
    rc = run_template(a["command"], args, 3600)
    sys.exit(rc)


def cmd_list():
    ms = load_manifests()
    rows = []
    for pid, m in ms.items():
        rows.append({"id": pid, "name": m["display_name"],
                     "status": m.get("status", "?"),
                     "capabilities": m.get("capabilities", []),
                     "auth": m.get("auth_method", "?"),
                     "actions": [a["id"] for a in m.get("actions", [])]})
    print(json.dumps({"bank": rows,
                      "total": len(rows),
                      "contribute": "add bank/connectors/<id>.json — see bank.py add-manifest"}, indent=2))


def cmd_status():
    ms = load_manifests()
    out = []
    for pid, m in ms.items():
        ok, msg = auth_state(m)
        out.append({"id": pid, "authorized": ok, "auth_state": msg,
                    "status": m.get("status", "?")})
    # engine readiness: graph tooling present
    engine = os.path.exists(os.path.join(HERE, "graph.py"))
    print(json.dumps({"connectors": out, "engine_ready": engine}, indent=2))


def add_manifest(path):
    if not os.path.exists(path):
        sys.exit(f"FATAL: no such file: {path}")
    try:
        d = json.load(open(path))
    except Exception as e:
        sys.exit(f"FATAL: invalid JSON: {e}")
    errs = validate_manifest(d)
    if errs:
        sys.exit("FATAL: manifest invalid — " + "; ".join(errs))
    os.makedirs(CONNECTOR_DIR, exist_ok=True)
    dest = os.path.join(CONNECTOR_DIR, f"{d['provider_id']}.json")
    if os.path.exists(dest):
        sys.exit(f"FATAL: {dest} exists (edit or remove it first)")
    with open(dest, "w") as f:
        json.dump(d, f, indent=2)
    print(json.dumps({"ok": True, "installed": dest,
                      "validate": "run bank.py status to check auth"}, indent=2))


def main():
    ap = argparse.ArgumentParser(description="The integration bank: connector registry + act/consume lanes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("status")
    p = sub.add_parser("consume")
    p.add_argument("connector"); p.add_argument("--db", default=None)
    p.add_argument("extra", nargs="*", help="extra args passed to the consume command")
    p2 = sub.add_parser("act")
    p2.add_argument("connector"); p2.add_argument("action")
    p2.add_argument("--args", default=None, help="JSON args for the action template")
    p2.add_argument("--db", default=None)
    p3 = sub.add_parser("add-manifest"); p3.add_argument("path")
    a = ap.parse_args()
    if a.cmd == "list":
        cmd_list()
    elif a.cmd == "status":
        cmd_status()
    elif a.cmd == "consume":
        consume(a.connector, a.extra, a.db)
    elif a.cmd == "act":
        act(a.connector, a.action, a.args, a.db)
    elif a.cmd == "add-manifest":
        add_manifest(a.path)


if __name__ == "__main__":
    main()