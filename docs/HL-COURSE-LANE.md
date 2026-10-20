# HighLevel course lane — the API pre-pass (`hl_course.py`)

Read-only HighLevel **Courses API v3** pre-pass that builds a complete,
correctly-ordered `course-map.json` _before_ any member-portal crawl.
Rationale (dossier §5.10): HL's tree (products → categories → lessons, with
`sequenceNo` and visibility/lock state) is fully API-readable even though
lesson _content_ is not — so the ingest dir carries a complete map even when
the cookie crawl later recovers only partial content.

Source study: the internal HighLevel membership-course UX study (2026-10-06)
(§1.2 endpoint schemas [SPEC], §3 ingest contract).

## Usage

```
python3 hl_course.py status                        # honest token state
python3 hl_course.py list LOCATION_ID              # courses for a location
python3 hl_course.py map PRODUCT_ID --location-id LOCATION_ID [--out-dir DIR]
```

`map` emits the normalized ingest-dir shape (`pages/ files/ media/ index.json`
— same as `course-dl.py`) plus `course-map.json`:

```json
{
  "schema": "course-map/highlevel-v1",
  "product": { "id": "...", "title": "...", "libraryOrder": 1 },
  "categories": [
    {
      "id": "...",
      "title": "Module 1",
      "lessons": [
        {
          "id": "...",
          "sequenceNo": 1,
          "visibility": "locked",
          "contentType": "video",
          "lockedByPost": null,
          "lockedByCategory": "...",
          "drip": null
        }
      ]
    }
  ],
  "totals": { "categories": 2, "lessons": 3, "published": 1, "draft": 1, "locked": 1 }
}
```

## Credentials (segmented store law)

Token path: `~/.consumer/creds/highlevel/token` (env `BRADAN_CREDS_ROOT`
relocates the root). Env `BRADAN_HL_TOKEN` is the documented fallback.

- HighLevel location-scoped token (OAuth "sub-account token" or Private
  Integration token), scope **`courses.readonly`**.
- The token is never printed, logged, or echoed — including in error paths
  (HTTP errors re-raise with the URL and body stripped, `creds.py` law).
- `status` reports **presence only** and exits nonzero when absent, with the
  exact remediation:

```
python3 hl_course.py status
{
  "platform": "highlevel",
  "authorized": false,
  "token_file": "~/.consumer/creds/highlevel/token",
  "token_file_present": false,
  "token_env": "BRADAN_HL_TOKEN",
  "token_env_set": false,
  "rate_limit_per_min": 80,
  "note": "token values are never printed",
  "remediation": "place the HighLevel location token (scope
  courses.readonly) at ~/.consumer/creds/highlevel/token, or export
  BRADAN_HL_TOKEN. See docs/HL-COURSE-LANE.md"
}
```

The HighLevel token **does not exist yet** on this machine. Until it
lands, `list`/`map` exit honestly (`not authorized`) rather than pretending.

## API contract (Courses v3)

All requests: `Authorization: Bearer <token>`, **`Version: v3`** header,
`locationId` query param, host `https://services.leadconnectorhq.com`.

| Call                                                               | Purpose                                                                                                                           |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| `GET /courses/products?locationId=&limit=&cursor=`                 | discover products (limit 1–50, `nextCursor` pagination)                                                                           |
| `GET /courses/products/:productId`                                 | the course object                                                                                                                 |
| `GET /courses/products/:productId/categories?locationId=`          | modules                                                                                                                           |
| `GET /courses/products/:productId/lessons?categoryId=&locationId=` | lessons, **always category-scoped** — the no-`categoryId` variant does N+1 internal membership reads (docs warning, dossier §1.2) |

Rate limit: **80 req/min** per location — a client-side sliding-window
limiter with sleep backoff is applied to every request.

## What is API-readable vs not (dossier §1.4 / §4)

Readable: tree UUIDs, titles, descriptions, poster images, `sequenceNo`,
visibility (`draft/published/locked`), lock refs (`lockedByPost`,
`lockedByCategory`), `contentType`, comment policy, progress/completions.

NOT readable (the hard boundary):

- lesson bodies / HTML
- video URLs (v3 has only `contentId`; `bucketVideoUrl` exists on the v2
  import _write_ shape only)
- member emails in enrollments; login tokens ("never returned")
- drip day-counts — a UI setting; no v3 endpoint documents a drip field.
  `course-map.json` emits `"drip": null` with a `drip_note` so downstream
  never mistakes absence for "immediate unlock".

**An API-only ingest is structurally impossible** — the member cookie crawl
remains mandatory for content.

## How the cookie-crawl half plugs in later

The API pre-pass is additive; the existing `course-dl.py` lane is untouched:

1. `map PRODUCT_ID` → ingest dir with complete `course-map.json` (this tool).
2. Crawl the member portal with a logged-in cookie jar scoped to the portal
   domain (`creds.py init highlevel` + `import`) — magic-link or
   email/password login → session cookie → `course-dl.py` into the same
   out-dir. This is the _only_ path to lesson bodies, Cloudflare Stream
   embeds (`highlevel` is already in `EMBED_HOSTS`), and `postMaterials`.
3. Reconcile: diff crawled lesson URLs against API lesson ids/titles to
   detect misses (the pattern HL's own Kajabi-importer uses).

`source.py` needs no lane-signature change: on a `members.*` portal URL, run
this pre-pass when a token is available, else pure crawl (dossier §3.3).

## Tests

`python3 tests/test_hl_lane.py` — fully mocked transport (no network, no
sleeps: the rate limiter runs on an injected fake clock). Covers request
shape (headers, `locationId`, category-scoping), pagination cursor
following, course-map emission shape and ordering, honest absent-token
status exit + message, rate-limiter backoff, and error hygiene (no
token/URL leakage through exceptions).
