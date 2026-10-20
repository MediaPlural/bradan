#!/usr/bin/env python3
"""consumer hl_course — HighLevel course-tree API pre-pass (Courses v3).

Builds the complete, correctly-ordered course map from HighLevel's Courses
API v3 (read-only) BEFORE any member-portal crawl. The dossier finding this
implements: HL's tree (products -> categories -> lessons, with sequenceNo and
visibility/lock state) is fully API-readable even though lesson *content* is
not — so the ingest dir gets a complete course-map.json even when the cookie
crawl later recovers only partial content. (HL-MEMBERSHIP-COURSE-UX-STUDY
2026-10-06, §3.1 and §5.10.)

Ingest contract (dossier §3):
  Discover: products -> categories -> lessons?categoryId= (per category; the
  no-categoryId variant does N+1 membership reads server-side — never use it).
  Emit: normalized ingest-dir shape (pages/ files/ media/ index.json) plus
  course-map.json carrying UUIDs, sequenceNo, visibility/lock state and
  content types. Lesson bodies, video URLs and member emails are NOT
  API-readable (dossier §1.4/§3.4) — the cookie crawl stays mandatory.

Auth (segmented store, creds.py law):
  Token at ~/.consumer/creds/highlevel/token (env BRADAN_CREDS_ROOT
  relocates the root), or env BRADAN_HL_TOKEN as documented fallback.
  Bearer auth, Version: v3 header, scope courses.readonly.
  The token is NEVER printed, logged, or echoed — including in errors
  (HTTP errors are re-raised with the query string stripped).

Rate limit: 80 req/min per location (published HL ceiling) — a simple
client-side limiter with sleep backoff is applied to every request.

Usage:
  python3 hl_course.py status                 # honest token state + remediation
  python3 hl_course.py list LOCATION_ID       # products for a location
  python3 hl_course.py map PRODUCT_ID --location-id L [--out-dir DIR]
                                             # emit course-map.json ingest dir
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://services.leadconnectorhq.com"
CREDS_ROOT = os.environ.get("BRADAN_CREDS_ROOT",
                            os.path.expanduser("~/.consumer/creds"))
TOKEN_PATH = os.path.join(CREDS_ROOT, "highlevel", "token")
TOKEN_ENV = "BRADAN_HL_TOKEN"
RATE_LIMIT_PER_MIN = 80          # published per-location ceiling (dossier §0)
PAGE_LIMIT = 50                  # v3 products/lessons limit is 1-50 [SPEC]
MAX_PAGES = 200                 # pagination runaway guard

# urllib is imported as a module; tests stub hl_course.urlopen directly.
urlopen = urllib.request.urlopen


# --------------------------------------------------------------------------
# credential boundary (creds.py law: only this function reads the token)
# --------------------------------------------------------------------------

def load_token():
    """Token from the segmented store, else env fallback. Returns None when
    absent. Values never leave this module except into the Authorization
    header inside _get_json()."""
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH) as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            pass
    return os.environ.get(TOKEN_ENV, "").strip() or None


def token_state():
    """Honest token presence — presence only, never the value."""
    return {"token_file": TOKEN_PATH,
            "token_file_present": os.path.exists(TOKEN_PATH),
            "token_env": TOKEN_ENV,
            "token_env_set": bool(os.environ.get(TOKEN_ENV, "").strip()),
            "authorized": False}


# --------------------------------------------------------------------------
# rate limiter — 80 req/min with sleep backoff (injectable clock for tests)
# --------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, max_per_min=RATE_LIMIT_PER_MIN,
                 clock=time.monotonic, sleep=time.sleep):
        self.max_per_min = max_per_min
        self.clock = clock
        self.sleep = sleep
        self.calls = []

    def acquire(self):
        """Block until one of the 80/min slots is free."""
        while True:
            now = self.clock()
            self.calls = [t for t in self.calls if now - t < 60.0]
            if len(self.calls) < self.max_per_min:
                self.calls.append(now)
                return
            wait = 60.0 - (now - self.calls[0]) + 0.01
            self.sleep(wait)


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

class HLApiError(Exception):
    """HTTP failure. Carries status only — the URL (query strings can carry
    tokens) and body never propagate, so nothing credential-shaped can leak
    into logs or tracebacks."""

    def __init__(self, status, message=""):
        self.status = status
        super().__init__(f"HighLevel API HTTP {status} {message}".strip())


def _get_json(path, params, token, limiter):
    """One authenticated v3 GET -> parsed JSON. Credentials attach HERE."""
    url = BASE_URL + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token,
                 "Version": "v3",
                 "Accept": "application/json",
                 "User-Agent": "consumer/0.1 hl_course"})
    limiter.acquire()
    try:
        with urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # redact exactly like creds.py: strip the URL, keep only the code
        raise HLApiError(e.code) from None
    except (urllib.error.URLError, OSError) as e:
        raise HLApiError(0, type(e).__name__) from None


# --------------------------------------------------------------------------
# ingest contract: products -> categories -> lessons (per category)
# --------------------------------------------------------------------------

def list_products(location_id, token, limiter=None):
    """GET /courses/products?locationId=… (limit/cursor pagination)."""
    limiter = limiter or RateLimiter()
    products, cursor, pages = [], None, 0
    while True:
        params = {"locationId": location_id, "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        data = _get_json("/courses/products", params, token, limiter)
        products.extend(data.get("products", []))
        cursor = data.get("nextCursor")
        pages += 1
        if not cursor or pages >= MAX_PAGES:
            return products


def fetch_categories(product_id, location_id, token, limiter):
    """GET /courses/products/:id/categories -> full category objects."""
    return _get_json(f"/courses/products/{product_id}/categories",
                     {"locationId": location_id},
                     token, limiter).get("categories", [])


def fetch_lessons_for_category(product_id, location_id, category_id,
                               token, limiter):
    """GET /courses/products/:id/lessons?categoryId=… — ALWAYS category-scoped
    (the no-categoryId variant does N+1 reads server-side; dossier §1.2)."""
    return _get_json(f"/courses/products/{product_id}/lessons",
                     {"locationId": location_id, "categoryId": category_id},
                     token, limiter).get("lessons", [])


def fetch_product(product_id, location_id, token, limiter):
    """GET /courses/products/:productId — the product (course) object."""
    return _get_json(f"/courses/products/{product_id}",
                     {"locationId": location_id}, token, limiter)


def build_course_map(product_id, location_id, token, limiter=None):
    """The full ordered tree: product -> categories -> lessons, lessons sorted
    by sequenceNo. Drip day-counts are NOT API-readable (dossier §4) —
    emitted as null with a note, so downstream never mistakes absence for
    'immediate unlock'."""
    limiter = limiter or RateLimiter()
    product = fetch_product(product_id, location_id, token, limiter)
    categories = fetch_categories(product_id, location_id, token, limiter)
    tree, totals = [], {"categories": 0, "lessons": 0,
                        "published": 0, "draft": 0, "locked": 0}
    for cat in categories:
        lessons = []
        for les in fetch_lessons_for_category(product_id, location_id,
                                              cat.get("id"), token, limiter):
            vis = les.get("visibility")
            totals[vis if vis in totals else "draft"] += 1
            lessons.append({
                "id": les.get("id"), "title": les.get("title"),
                "sequenceNo": les.get("sequenceNo"),
                "visibility": vis,
                "contentType": les.get("contentType"),
                "commentStatus": les.get("commentStatus"),
                "commentPermission": les.get("commentPermission"),
                "lockedByPost": les.get("lockedByPost"),
                "lockedByCategory": les.get("lockedByCategory"),
                "description": les.get("description"),
                "posterImage": les.get("posterImage"),
                "contentId": les.get("contentId"),
                "drip": None,   # not API-readable — see drip_note
                "createdAt": les.get("createdAt"),
                "updatedAt": les.get("updatedAt")})
        lessons.sort(key=lambda l: (l["sequenceNo"] is None,
                                   l["sequenceNo"] if l["sequenceNo"] is not None else 0))
        totals["categories"] += 1
        totals["lessons"] += len(lessons)
        tree.append({"id": cat.get("id"), "title": cat.get("title"),
                     "visibility": cat.get("visibility"),
                     "lessons": lessons})
    return {
        "schema": "course-map/highlevel-v1",
        "source": "highlevel",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "location_id": location_id,
        "product": {"id": product.get("id"), "title": product.get("title"),
                    "description": product.get("description"),
                    "posterImage": product.get("posterImage"),
                    "libraryOrder": product.get("libraryOrder"),
                    "createdAt": product.get("createdAt"),
                    "updatedAt": product.get("updatedAt")},
        "categories": tree,
        "totals": totals,
        "drip_note": ("drip day-counts are a UI setting, not API-readable "
                      "(dossier §4); lock state is mirrored via "
                      "visibility/lockedByPost/lockedByCategory"),
        "credential": "platform:highlevel"}


def emit_ingest_dir(course_map, out_dir):
    """Normalized ingest-dir shape (course-dl.py compatible): pages/ files/
    media/ + index.json, plus course-map.json as the additive API metadata.
    distill/sitegen pick this up with zero lane changes."""
    for sub in ("pages", "files", "media"):
        os.makedirs(os.path.join(out_dir, sub), exist_ok=True)
    map_path = os.path.join(out_dir, "course-map.json")
    with open(map_path, "w") as f:
        json.dump(course_map, f, indent=2)
    index = {"root": "highlevel://products/" + str(course_map["product"]["id"]),
             "pages": [], "files": [], "media": [],
             "course_map": "course-map.json"}
    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump(index, f, indent=2)
    return map_path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _require_token(what):
    tok = load_token()
    if not tok:
        st = token_state()
        print(json.dumps({"ok": False, "error": "not authorized",
                          "status": st,
                          "remediation": (
                              "place the HighLevel location "
                              "token (scope courses.readonly) at "
                              f"{TOKEN_PATH} — or export {TOKEN_ENV} — "
                              "then re-run this command. "
                              "See docs/HL-COURSE-LANE.md")}, indent=2))
        sys.exit(2)
    return tok


def cmd_status():
    """Honest status, bank.py law: presence only, exact remediation, never
    the value. Exit 0 when authorized, 1 when not."""
    st = token_state()
    authorized = st["token_file_present"] or st["token_env_set"]
    st["authorized"] = authorized
    out = {"platform": "highlevel", "authorized": authorized,
           "token_file": TOKEN_PATH,
           "token_file_present": st["token_file_present"],
           "token_env": TOKEN_ENV,
           "token_env_set": st["token_env_set"],
           "rate_limit_per_min": RATE_LIMIT_PER_MIN,
           "note": "token values are never printed"}
    if not authorized:
        out["remediation"] = ("place the HighLevel location "
                              f"token (scope courses.readonly) at "
                              f"{TOKEN_PATH}, or export {TOKEN_ENV}. "
                              "See docs/HL-COURSE-LANE.md")
    print(json.dumps(out, indent=2))
    sys.exit(0 if authorized else 1)


def cmd_list(location_id):
    token = _require_token("list " + location_id)
    products = list_products(location_id, token)
    print(json.dumps({"location_id": location_id,
                      "products": [{"id": p.get("id"), "title": p.get("title"),
                                    "description": p.get("description"),
                                    "createdAt": p.get("createdAt")}
                                   for p in products],
                      "total": len(products)}, indent=2))


def cmd_map(product_id, location_id, out_dir):
    token = _require_token("map " + product_id)
    out_dir = out_dir or f"hl-course-{product_id[:8] if product_id else 'map'}"
    course_map = build_course_map(product_id, location_id, token)
    map_path = emit_ingest_dir(course_map, out_dir)
    print(json.dumps({"ok": True,
                      "out_dir": os.path.abspath(out_dir),
                      "course_map": os.path.abspath(map_path),
                      "categories": course_map["totals"]["categories"],
                      "lessons": course_map["totals"]["lessons"],
                      "next": ("crawl the member portal with the cookie jar "
                               "(course-dl.py) into the same out-dir — "
                               "lesson bodies are not API-readable")},
                     indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="HighLevel course-tree API pre-pass (Courses v3, read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="honest token state + remediation")
    p_list = sub.add_parser("list", help="list products for a location")
    p_list.add_argument("location_id")
    p_map = sub.add_parser("map", help="emit course-map.json for a product")
    p_map.add_argument("product_id")
    p_map.add_argument("--location-id", required=True,
                       help="HighLevel location (sub-account) id")
    p_map.add_argument("--out-dir", default=None,
                       help="ingest dir to emit (default ./hl-course-<id>)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            cmd_status()
        elif a.cmd == "list":
            cmd_list(a.location_id)
        elif a.cmd == "map":
            cmd_map(a.product_id, a.location_id, a.out_dir)
    except HLApiError as e:
        # status/code only — no URL, no body, no token in the message
        if e.status == 401:
            print(json.dumps({"ok": False, "error": "HL API rejected the token",
                              "http": 401,
                              "remediation": "token invalid or missing "
                                             "courses.readonly scope — "
                                             "re-issue the token"},
                             indent=2), file=sys.stderr)
        else:
            print(json.dumps({"ok": False, "error": "HL API request failed",
                              "http": e.status}, indent=2), file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()