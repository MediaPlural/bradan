#!/usr/bin/env python3
"""tests for hl_course — the HighLevel course-tree API pre-pass lane.

ALL network transport is mocked: hl_course.urlopen is replaced with a stub
that records request shape (headers, query) and serves the dossier's
documented response shapes. No real network calls, no fake 200s in prod code
(prod hits the real API or exits honest). The rate limiter is tested with an
injected fake clock so no test ever sleeps.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hl_course  # noqa: E402

_ORIGINAL_MODULE = hl_course
import importlib  # noqa: E402


def _reload_hl():
    """Fresh module state so per-test CREDS_ROOT/token env changes apply."""
    global hl_course
    hl_course = importlib.reload(hl_course)
    return hl_course

LOCATION = "loc-ABC123"
PRODUCT = "11111111-2222-3333-4444-555555555555"
CAT_A = "aaaaaaaa-0000-0000-0000-000000000001"
CAT_B = "bbbbbbbb-0000-0000-0000-000000000002"
TOKEN = "tok-should-never-appear-in-any-output"


class FakeResponse:
    def __init__(self, payload):
        self._buf = io.BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class StubTransport:
    """Replaces hl_course.urlopen. Records every Request; serves canned JSON
    by matching path (and optional query)."""

    def __init__(self):
        self.requests = []          # list of (url, headers dict)
        self.routes = {}            # (path, optional categoryId) -> payload | list
        self._seq = {}

    def add(self, path, payload, **query_match):
        self.routes[(path, query_match.get("categoryId"))] = payload

    def __call__(self, req, timeout=None):
        parsed = urllib.parse.urlsplit(req.full_url)
        q = urllib.parse.parse_qs(parsed.query)
        self.requests.append((req.full_url, dict(req.header_items()),
                              parsed.path, q))
        key = (parsed.path, q.get("categoryId", [None])[0])
        route = self.routes.get(key)
        if route is None:
            raise urllib.error.HTTPError(req.full_url, 404, "not stubbed",
                                         None, None)
        if isinstance(route, list):
            # sequential payloads (pagination): pop next, repeat last
            n = self._seq.get(key, 0)
            self._seq[key] = n + 1
            route = route[n] if n < len(route) else route[-1]
        return FakeResponse(route)


class NoSleepClock:
    """Fake monotonic clock + sleep recorder for rate-limiter tests."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.sleeps.append(round(secs, 2))
        self.now += secs


class Unit(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # isolate the segmented creds root per test, then reload so the
        # module's TOKEN_PATH points at this test's store
        os.environ["BRADAN_CREDS_ROOT"] = self.tmp.name
        os.environ.pop("BRADAN_HL_TOKEN", None)
        global hl_course
        hl_course = _reload_hl()
        self.stub = StubTransport()
        hl_course.urlopen = self.stub

    def tearDown(self):
        hl_course = _ORIGINAL_MODULE
        self.tmp.cleanup()

    def token_present(self, tmp_root=None):
        root = tmp_root or self.tmp.name
        os.makedirs(os.path.join(root, "highlevel"), exist_ok=True)
        with open(os.path.join(root, "highlevel", "token"), "w") as f:
            f.write(TOKEN + "\n")
        return root


# ---------------------------------------------------------------- request shape

class TestRequestShape(Unit):

    def test_products_request_headers_and_query(self):
        self.token_present()
        self.stub.add("/courses/products", {"products": [], "nextCursor": None})
        hl_course.list_products(LOCATION, hl_course.load_token())
        url, headers, path, q = self.stub.requests[0]
        self.assertEqual(path, "/courses/products")
        self.assertEqual(headers.get("Version"), "v3")
        self.assertTrue(headers.get("Authorization", "").startswith("Bearer "))
        self.assertIn(TOKEN, headers.get("Authorization", ""))
        self.assertEqual(q["locationId"], [LOCATION])
        self.assertEqual(q["limit"], ["50"])
        # the token must appear in the URL nowhere (query strings get logged)
        self.assertNotIn(TOKEN, url)

    def test_categories_request_shape(self):
        self.token_present()
        self.stub.add(f"/courses/products/{PRODUCT}/categories",
                      {"categories": []})
        hl_course.fetch_categories(PRODUCT, LOCATION, hl_course.load_token(),
                                   hl_course.RateLimiter())
        url, headers, path, q = self.stub.requests[0]
        self.assertEqual(path, f"/courses/products/{PRODUCT}/categories")
        self.assertEqual(headers.get("Version"), "v3")
        self.assertEqual(q["locationId"], [LOCATION])

    def test_lessons_are_always_category_scoped(self):
        """Dossier §1.2: the no-categoryId lessons variant does N+1 server
        reads — every lesson fetch MUST carry categoryId."""
        self.token_present()
        self.stub.add(f"/courses/products/{PRODUCT}/lessons",
                      {"lessons": []}, categoryId=CAT_A)
        hl_course.fetch_lessons_for_category(PRODUCT, LOCATION, CAT_A,
                                             hl_course.load_token(),
                                             hl_course.RateLimiter())
        url, headers, path, q = self.stub.requests[0]
        self.assertEqual(path, f"/courses/products/{PRODUCT}/lessons")
        self.assertEqual(q["categoryId"], [CAT_A])
        self.assertEqual(q["locationId"], [LOCATION])
        self.assertEqual(headers.get("Version"), "v3")

    def test_product_get_request_shape(self):
        self.token_present()
        self.stub.add(f"/courses/products/{PRODUCT}", {"id": PRODUCT})
        hl_course.fetch_product(PRODUCT, LOCATION, hl_course.load_token(),
                                 hl_course.RateLimiter())
        url, headers, path, q = self.stub.requests[0]
        self.assertEqual(path, f"/courses/products/{PRODUCT}")
        self.assertEqual(q["locationId"], [LOCATION])


# ------------------------------------------------------------------- pagination

class TestPagination(Unit):

    def test_products_cursor_follows_next_cursor(self):
        self.token_present()
        self.stub.add("/courses/products", [
            {"products": [{"id": "p1", "title": "A"}],
             "nextCursor": "cur-1"},
            {"products": [{"id": "p2", "title": "B"}],
             "nextCursor": "cur-2"},
            {"products": [{"id": "p3", "title": "C"}], "nextCursor": None},
        ])
        products = hl_course.list_products(LOCATION, hl_course.load_token())
        self.assertEqual([p["id"] for p in products], ["p1", "p2", "p3"])
        self.assertEqual(len(self.stub.requests), 3)
        cursors = [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
                   .get("cursor", [None])[0]
                   for u, *_ in self.stub.requests]
        self.assertEqual(cursors, [None, "cur-1", "cur-2"])


# ------------------------------------------------------------- course-map shape

def _route_full_tree(stub):
    stub.add("/courses/products/" + PRODUCT,
             {"id": PRODUCT, "title": "Test Course",
              "description": "d", "libraryOrder": 1})
    stub.add(f"/courses/products/{PRODUCT}/categories",
             {"categories": [
                 {"id": CAT_A, "title": "Module 1", "visibility": "published"},
                 {"id": CAT_B, "title": "Module 2", "visibility": "draft"}]})
    stub.add(f"/courses/products/{PRODUCT}/lessons",
             {"lessons": [
                 {"id": "l2", "title": "Lesson 2", "sequenceNo": 2,
                  "visibility": "published", "contentType": "video",
                  "lockedByPost": "l1", "commentStatus": "visible",
                  "commentPermission": "enabled"},
                 {"id": "l1", "title": "Lesson 1", "sequenceNo": 1,
                  "visibility": "locked", "contentType": "video",
                  "lockedByCategory": CAT_A,
                  "commentStatus": "locked", "commentPermission": "locked"}]},
             categoryId=CAT_A)
    stub.add(f"/courses/products/{PRODUCT}/lessons",
             {"lessons": [
                 {"id": "l3", "title": "Lesson 3", "sequenceNo": 1,
                  "visibility": "draft", "contentType": "quiz"}]},
             categoryId=CAT_B)


class TestCourseMap(Unit):

    def test_map_full_tree_shape_and_ordering(self):
        self.token_present()
        _route_full_tree(self.stub)
        cmap = hl_course.build_course_map(PRODUCT, LOCATION,
                                          hl_course.load_token())
        self.assertEqual(cmap["schema"], "course-map/highlevel-v1")
        self.assertEqual(cmap["product"]["id"], PRODUCT)
        self.assertEqual(cmap["product"]["title"], "Test Course")
        self.assertEqual([c["title"] for c in cmap["categories"]],
                         ["Module 1", "Module 2"])
        # lessons sorted by sequenceNo within each category
        m1 = cmap["categories"][0]["lessons"]
        self.assertEqual([l["id"] for l in m1], ["l1", "l2"])
        self.assertEqual(m1[0]["visibility"], "locked")
        self.assertEqual(m1[0]["lockedByCategory"], CAT_A)
        self.assertEqual(m1[1]["lockedByPost"], "l1")
        m2 = cmap["categories"][1]["lessons"]
        self.assertEqual([l["id"] for l in m2], ["l3"])
        self.assertEqual(m2[0]["contentType"], "quiz")
        self.assertEqual(cmap["totals"], {"categories": 2, "lessons": 3,
                                         "published": 1, "draft": 1, "locked": 1})
        # drip is not API-readable — explicit null, never a fake 0
        self.assertIsNone(m1[0]["drip"])
        self.assertIn("not API-readable", cmap["drip_note"])

    def test_lesson_fetches_hit_every_category(self):
        self.token_present()
        _route_full_tree(self.stub)
        hl_course.build_course_map(PRODUCT, LOCATION, hl_course.load_token())
        lesson_calls = [(p, q.get("categoryId", [None])[0])
                        for _u, _h, p, q in self.stub.requests
                        if p.endswith("/lessons")]
        self.assertEqual(lesson_calls,
                         [(f"/courses/products/{PRODUCT}/lessons", CAT_A),
                          (f"/courses/products/{PRODUCT}/lessons", CAT_B)])

    def test_emit_ingest_dir_shape(self):
        self.token_present()
        _route_full_tree(self.stub)
        cmap = hl_course.build_course_map(PRODUCT, LOCATION,
                                          hl_course.load_token())
        out = os.path.join(self.tmp.name, "ingest")
        map_path = hl_course.emit_ingest_dir(cmap, out)
        self.assertTrue(os.path.exists(map_path))
        for sub in ("pages", "files", "media"):
            self.assertTrue(os.path.isdir(os.path.join(out, sub)))
        with open(os.path.join(out, "index.json")) as f:
            index = json.load(f)
        self.assertEqual(index["course_map"], "course-map.json")
        self.assertIn("pages", index)
        with open(map_path) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["product"]["id"], PRODUCT)
        # credential identity only — never the token
        raw = open(map_path).read()
        self.assertNotIn(TOKEN, raw)


# ------------------------------------------------------------ honest status

class TestHonestStatus(Unit):

    def run_status_with_no_token(self):
        os.environ.pop("BRADAN_HL_TOKEN", None)
        root = os.path.join(self.tmp.name, "empty-creds")
        os.makedirs(os.path.join(root, "highlevel"), exist_ok=True)
        os.environ["BRADAN_CREDS_ROOT"] = root
        # reload module state under the isolated creds root
        import importlib
        global hl_course
        hl_course = importlib.reload(hl_course)
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                hl_course.cmd_status()
        return cm.exception.code, json.loads(buf.getvalue())

    def test_status_exit_nonzero_with_remediation_when_absent(self):
        code, out = self.run_status_with_no_token()
        self.assertNotEqual(code, 0)
        self.assertFalse(out["authorized"])
        self.assertFalse(out["token_file_present"])
        self.assertIn("remediation", out)
        self.assertIn("courses.readonly", out["remediation"])
        self.assertIn("token", out["remediation"])
        # the token path is named; the token itself never exists in this env
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_require_token_exits_with_remediation_when_absent(self):
        os.environ.pop("BRADAN_HL_TOKEN", None)
        os.environ["BRADAN_CREDS_ROOT"] = os.path.join(self.tmp.name, "x2")
        import importlib
        global hl_course
        hl_course = importlib.reload(hl_course)
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                hl_course._require_token("list " + LOCATION)
        self.assertEqual(cm.exception.code, 2)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["error"], "not authorized")
        self.assertIn("remediation", out)
        self.assertIn("courses.readonly", out["remediation"])

    def test_status_zero_when_token_present(self):
        root = self.token_present()
        os.environ["BRADAN_CREDS_ROOT"] = root
        import importlib
        global hl_course
        hl_course = importlib.reload(hl_course)
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                hl_course.cmd_status()
        self.assertEqual(cm.exception.code, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["authorized"])
        self.assertTrue(out["token_file_present"])
        self.assertNotIn(TOKEN, buf.getvalue())

    def test_env_token_fallback_documented(self):
        os.environ["BRADAN_HL_TOKEN"] = "env-tok"
        os.environ["BRADAN_CREDS_ROOT"] = os.path.join(self.tmp.name, "x3")
        import importlib
        global hl_course
        hl_course = importlib.reload(hl_course)
        self.assertEqual(hl_course.load_token(), "env-tok")


# ------------------------------------------------------------ rate limiter

class TestRateLimiter(Unit):

    def test_under_limit_never_sleeps(self):
        clock = NoSleepClock()
        rl = hl_course.RateLimiter(clock=clock.monotonic, sleep=clock.sleep)
        for i in range(80):
            rl.acquire()
            clock.now += 0.5   # 80 calls in 40s — under 80/min
        self.assertEqual(clock.sleeps, [])

    def test_over_limit_sleeps_until_slot_frees(self):
        clock = NoSleepClock()
        rl = hl_course.RateLimiter(clock=clock.monotonic, sleep=clock.sleep)
        for i in range(80):
            rl.acquire()
            clock.now += 0.1   # 80 calls inside 8s
        self.assertEqual(clock.sleeps, [])
        rl.acquire()           # 81st inside the window — must back off
        self.assertEqual(len(clock.sleeps), 1)
        # 80 calls at t=0.0..7.9; the 81st fires at t=8.0 and must wait for
        # the oldest (t=0.0) to age out of the 60s window
        expected = 60.0 - (80 * 0.1) + 0.01
        self.assertAlmostEqual(clock.sleeps[0], expected, places=2)
        # after the backoff, more calls succeed (window has slid)
        for _ in range(10):
            rl.acquire()
            clock.now += 0.1
        self.assertLessEqual(len(clock.sleeps), 2)


# --------------------------------------------------------------- error hygiene

class TestErrorHygiene(Unit):

    def test_http_error_carries_no_url_or_token(self):
        self.token_present()

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url + "?token=secret", 401, "Bad Request", None, None)
        hl_course.urlopen = boom
        with self.assertRaises(hl_course.HLApiError) as cm:
            hl_course.list_products(LOCATION, hl_course.load_token())
        err = str(cm.exception)
        self.assertIn("401", err)
        self.assertNotIn(TOKEN, err)
        self.assertNotIn("secret", err)
        self.assertNotIn("http", str(cm.exception.__cause__ or ""))

    def test_404_from_stub_surfaces_as_hl_api_error(self):
        self.token_present()
        with self.assertRaises(hl_course.HLApiError) as cm:
            hl_course.list_products(LOCATION, hl_course.load_token())
        self.assertEqual(cm.exception.status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)