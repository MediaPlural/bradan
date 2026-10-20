#!/usr/bin/env python3
"""bradan connectors — "consume my <app> archive" via real auth (OAuth/IMAP).

The agent-native app layer: every connector knows how to (1) AUTHENTICATE —
via ortie (the OAuth token manager) or an
app password — and (2) EXPORT an archive, which then flows through the
standard engine (ingest -> graph, attributed to the connector).

The credential law holds: tokens never appear in command lines, logs, or
this file. Ortie mints them; the engine consumes them in-process.

Connectors (grow the list per app):
  gmail      Gmail / Google Workspace (ortie Gmail-API account, or IMAP)
  imap       any IMAP mailbox (app password)
  drive      Google Drive (via ortie google account) — files list + download

Usage:
  python3 connectors.py list
  python3 connectors.py auth gmail            # one-time: open the OAuth flow
  python3 connectors.py export gmail --since 2026-01-01 --db GRAPH.db
  python3 connectors.py export imap --host imap.gmail.com --user you@gmail.com \\
      --out ./mail-archive [--db GRAPH.db]     # app password via keychain/env
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[connector] {msg}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------- ortie lane

def nango_token(connection_id, nango_url=None, nango_key=None):
    """Access token from a SELF-HOSTED Nango instance (open source — you own
    custody). Never a SaaS token: the consumer's integration law is local
    custody; Nango is adopted as optional prebuilt-OAuth plumbing for the
    long tail of apps we won't hand-wire. URL from env or --nango-url; the
    secret key lives in env BRADAN_NANGO_KEY (never argv)."""
    url = nango_url or os.environ.get("BRADAN_NANGO_URL")
    key = nango_key or os.environ.get("BRADAN_NANGO_KEY")
    if not url or not key:
        return None
    req = urllib.request.Request(
        f"{url.rstrip('/')}/connection/{connection_id}",
        headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        return (d.get("credentials") or {}).get("access_token")
    except Exception:
        return None


def ortie_token(account):
    """Fresh access token from ortie (refresh if needed). Never logged."""
    r = subprocess.run(["ortie", "token", "show", "--account", account],
                       capture_output=True, text=True, timeout=120)
    tok = (r.stdout or "").strip()
    return tok if (r.returncode == 0 and tok) else None


def gmail_export(account, since, out_dir, db, limit):
    """Gmail API via ortie token. Streams message text into a staging dir,
    then ingests+graphs it. Attribution: gmail:<account>."""
    tok = ortie_token(account)
    if not tok:
        sys.exit(f"FATAL: no ortie token for '{account}' — run: connectors.py auth gmail")
    os.makedirs(out_dir, exist_ok=True)

    # list message ids since date (page through; stop at limit)
    q = f"after:{since.replace('-', '/')}" if since else "in:anywhere"
    ids, page = [], None
    while True:
        url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=100&q="
               + urllib.request.quote(q))
        if page:
            url += "&pageToken=" + page
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        batch = [m["id"] for m in d.get("messages", [])]
        ids.extend(batch)
        page = d.get("nextPageToken")
        if not page or (limit and len(ids) >= limit):
            break
    ids = ids[:limit] if limit else ids
    log(f"{len(ids)} messages since {since or 'forever'}")

    # fetch + extract text per message
    def get(mid):
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=full"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    n = 0
    for i, mid in enumerate(ids):
        try:
            m = get(mid)
        except Exception as e:
            log(f"skip {mid}: {e}")
            continue
        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "(no subject)")
        frm = headers.get("from", "")
        date = headers.get("date", "")
        body_parts = []
        # collect from payload tree (multipart) or the top-level body
        def walk_parts(p):
            if not isinstance(p, dict):
                return
            if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
                body_parts.append(base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace"))
            for sub in p.get("parts", []) or []:
                walk_parts(sub)
        walk_parts(m.get("payload", {}))
        body = "\n".join(body_parts) or "(no plain-text body)"
        # strip tracking pixels/list-unsubscribe noise: URLs that dominate
        # machine mail but carry zero knowledge
        body = "\n".join(ln for ln in body.splitlines()
                         if not (ln.strip().startswith("http") and len(ln.split()) == 1))
        fname = f"msg-{i:05d}.txt"
        with open(os.path.join(out_dir, fname), "w") as f:
            f.write(f"From: {frm}\nDate: {date}\nSubject: {subject}\n\n{body}")
        n += 1
    # acquisition manifest = attribution: which account, what window
    with open(os.path.join(out_dir, "acquisition.json"), "w") as f:
        json.dump({"sources": [
            {"kind": "email", "url": None, "file": os.path.join(out_dir, f"msg-{i:05d}.txt"),
             "sha256": "0" * 64, "tool": f"gmail:{account}", "size": 0,
             "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            for i in range(n)]}, f)
    log(f"exported {n} messages -> {out_dir}")

    if db:
        r = subprocess.run(["python3", os.path.join(HERE, "ingest.py"), out_dir, "--recursive"],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            sys.exit(f"FATAL: ingest failed:\n{(r.stderr or '')[-300:]}")
        r2 = subprocess.run(["python3", os.path.join(HERE, "graph.py"), "ingest",
                             os.path.join(out_dir, "extracted"), "--db", db],
                            capture_output=True, text=True, timeout=3600)
        if r2.returncode != 0:
            sys.exit(f"FATAL: graph failed:\n{(r2.stderr or '')[-300:]}")
        print(json.dumps({"ok": True, "connector": "gmail", "account": account,
                          "messages": n, "graph": json.loads(r2.stdout)}, indent=2))
    else:
        print(json.dumps({"ok": True, "connector": "gmail", "account": account,
                          "messages": n, "out_dir": os.path.abspath(out_dir)}, indent=2))


def imap_export(host, user, out_dir, db, limit, folder):
    """Any IMAP mailbox with an app password. Password NEVER on the command
    line: read from env BRADAN_IMAP_PASS or macOS keychain (security find-
    generic-password -s consumer-imap)."""
    import imaplib
    import email as email_lib
    pw = os.environ.get("BRADAN_IMAP_PASS")
    if not pw:
        r = subprocess.run(["security", "find-generic-password", "-s", "consumer-imap", "-w"],
                           capture_output=True, text=True, timeout=30)
        pw = (r.stdout or "").strip() or None
    if not pw:
        sys.exit("FATAL: no IMAP password — set BRADAN_IMAP_PASS or store it:\n"
                 "  security add-generic-password -s consumer-imap -a you@gmail.com -w")
    os.makedirs(out_dir, exist_ok=True)
    M = imaplib.IMAP4_SSL(host)
    M.login(user, pw)
    M.select(folder, readonly=True)
    typ, data = M.search(None, "ALL")
    ids = data[0].split()
    ids = ids[-limit:] if limit else ids
    n = 0
    for i, num in enumerate(ids):
        typ, msgdata = M.fetch(num, "(RFC822)")
        if typ != "OK":
            continue
        raw = msgdata[0][1]
        msg = email_lib.message_from_bytes(raw)
        body = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body.append(part.get_payload(decode=True).decode("utf-8", "replace"))
        else:
            body.append(msg.get_payload(decode=True).decode("utf-8", "replace") or "")
        with open(os.path.join(out_dir, f"msg-{i:05d}.txt"), "w") as f:
            f.write(f"From: {msg.get('From', '')}\nDate: {msg.get('Date', '')}\n"
                    f"Subject: {msg.get('Subject', '')}\n\n" + "\n".join(body))
        n += 1
    with open(os.path.join(out_dir, "acquisition.json"), "w") as f:
        json.dump({"sources": [
            {"kind": "email", "url": None, "file": os.path.join(out_dir, f"msg-{i:05d}.txt"),
             "sha256": "0" * 64, "tool": f"imap:{host}:{user}", "size": 0,
             "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")} for i in range(n)]}, f)
    M.logout()
    log(f"exported {n} messages from {folder}")
    if db:
        subprocess.run(["python3", os.path.join(HERE, "ingest.py"), out_dir, "--recursive"],
                       capture_output=True, text=True, timeout=3600)
        r2 = subprocess.run(["python3", os.path.join(HERE, "graph.py"), "ingest",
                             os.path.join(out_dir, "extracted"), "--db", db],
                            capture_output=True, text=True, timeout=3600)
        print(json.dumps({"ok": True, "connector": "imap", "host": host, "user": user,
                          "messages": n, "graph": json.loads(r2.stdout) if r2.returncode == 0 else None}, indent=2))
    else:
        print(json.dumps({"ok": True, "connector": "imap", "host": host, "user": user,
                          "messages": n, "out_dir": os.path.abspath(out_dir)}, indent=2))




def drive_export(account, out_dir, db, limit, query):
    """Google Drive files (docs/sheets/pdfs) via ortie google account (or
    Nango connection 'google-drive'). Lists files matching query, downloads
    each (export mime for docs), drops them in out_dir with attribution."""
    tok = ortie_token(account) or nango_token("google-drive")
    if not tok:
        sys.exit("FATAL: no google token — run ortie configure with Drive scopes, "
                 "or set BRADAN_NANGO_URL/KEY for a Nango google-drive connection")
    os.makedirs(out_dir, exist_ok=True)
    q = urllib.request.quote(query)
    url = (f"https://www.googleapis.com/drive/v3/files?q={q}"
           f"&pageSize={min(limit or 100, 1000)}&fields=files(id,name,mimeType,size)")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("FATAL: token lacks Drive scope — the ortie account needs "
                     "googleapis.com/auth/drive.readonly (re-run ortie configure "
                     "with Drive scopes) or use a Nango google-drive connection")
        raise
    files = d.get("files", [])[:limit] if limit else d.get("files", [])
    log(f"{len(files)} drive files")
    manifest = []
    for i, f in enumerate(files):
        if f.get("mimeType") in ("application/vnd.google-apps.document",):
            export = "text/plain"
        elif f.get("mimeType") == "application/vnd.google-apps.spreadsheet":
            export = "text/csv"
        else:
            export = None
        try:
            if export:
                u = f"https://www.googleapis.com/drive/v3/files/{f['id']}/export?exportFormat={urllib.request.quote(export)}"
            else:
                u = f"https://www.googleapis.com/drive/v3/files/{f['id']}?alt=media"
            req = urllib.request.Request(u, headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            safe = "".join(c if c.isalnum() or c in "._ -" else "_" for c in f["name"])[:80]
            path = os.path.join(out_dir, f"{i:04d}-{safe}.bin")
            with open(path, "wb") as fh:
                fh.write(data)
            manifest.append({"kind": "drive-file", "url": f"https://drive.google.com/file/d/{f['id']}/view",
                             "file": path, "sha256": "0" * 64, "tool": f"drive:{account}",
                             "size": len(data), "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        except Exception as e:
            log(f"skip {f.get('name')}: {e}")
    with open(os.path.join(out_dir, "acquisition.json"), "w") as fh:
        json.dump({"sources": manifest}, fh)
    log(f"downloaded {len(manifest)} files")
    if db and manifest:
        r = subprocess.run(["python3", os.path.join(HERE, "ingest.py"), out_dir, "--recursive"],
                           capture_output=True, text=True, timeout=3600)
        r2 = subprocess.run(["python3", os.path.join(HERE, "graph.py"), "ingest",
                             os.path.join(out_dir, "extracted"), "--db", db],
                            capture_output=True, text=True, timeout=3600)
        print(json.dumps({"ok": True, "connector": "drive", "files": len(manifest),
                          "graph": json.loads(r2.stdout) if r2.returncode == 0 else None}, indent=2))
    else:
        print(json.dumps({"ok": True, "connector": "drive", "files": len(manifest),
                          "out_dir": os.path.abspath(out_dir)}, indent=2))


CONNECTORS = {
    "gmail": {"auth": "ortie (OAuth; Gmail API) or Nango", "export": "messages since date -> graph"},
    "imap": {"auth": "app password (env/keychain)", "export": "any IMAP mailbox -> graph"},
    "drive": {"auth": "ortie google account or Nango google-drive", "export": "docs/sheets/pdfs -> graph"},
}


def main():
    ap = argparse.ArgumentParser(description="App connectors: 'consume my <app> archive' with real auth")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("auth"); p.add_argument("connector", choices=list(CONNECTORS))
    p = sub.add_parser("export"); p.add_argument("connector", choices=list(CONNECTORS))
    p.add_argument("--query", default="trashed = false", help="drive: files.list query")
    p.add_argument("--account", default="default", help="ortie account (gmail)")
    p.add_argument("--since", default=None, help="YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", default=None, help="export dir (default ./mail-archive)")
    p.add_argument("--db", default=None, help="graph db to feed")
    # imap-specific
    p.add_argument("--host", default="imap.gmail.com")
    p.add_argument("--user", default=None)
    p.add_argument("--folder", default="INBOX")
    a = ap.parse_args()

    if a.cmd == "list":
        print(json.dumps(CONNECTORS, indent=2))
    elif a.cmd == "auth":
        if a.connector == "gmail":
            r = subprocess.run(["ortie", "auth", "--account", a.account],
                               capture_output=False, timeout=600)
            log(f"ortie auth flow completed for '{a.account}'")
        else:
            log(f"{a.connector}: auth happens at first export (keychain/env)")
    elif a.cmd == "export":
        out = a.out or "./mail-archive"
        if a.connector == "gmail":
            gmail_export(a.account, a.since, out, a.db, a.limit)
        elif a.connector == "drive":
            drive_export(a.account, out, a.db, a.limit, a.query)
        elif a.connector == "imap":
            if not a.user:
                sys.exit("FATAL: imap needs --user you@example.com")
            imap_export(a.host, a.user, out, a.db, a.limit, a.folder)


if __name__ == "__main__":
    main()