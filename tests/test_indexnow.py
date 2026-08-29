"""Offline unit tests for indexnow.py. No network. Run:  python -m unittest discover tests"""

import gzip
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import indexnow  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def read_fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def urlmap(xml_name):
    kind, items = indexnow.parse_sitemap(read_fixture(xml_name))
    assert kind == "urlset"
    return {loc: (lm or "") for loc, lm in items}


LIMITS = {
    "circuit_breaker_min": 5000,
    "circuit_breaker_fraction": 0.25,
    "per_run_cap": 10000,
    "debounce_hours": 24,
}


class TestParsing(unittest.TestCase):
    def test_parse_index(self):
        kind, items = indexnow.parse_sitemap(read_fixture("index.xml"))
        self.assertEqual(kind, "index")
        self.assertEqual(items, [
            "https://pharmeasy.in/sitemaps/child-a.xml",
            "https://pharmeasy.in/sitemaps/child-b.xml",
        ])

    def test_parse_urlset_with_image_ext(self):
        kind, items = indexnow.parse_sitemap(read_fixture("urlset_v1.xml"))
        self.assertEqual(kind, "urlset")
        locs = [loc for loc, _ in items]
        self.assertIn("https://pharmeasy.in/online-medicine-order/changing-product-2", locs)
        self.assertEqual(len(items), 4)  # image children must not add rows

    def test_parse_rejects_unknown_root(self):
        with self.assertRaises(ValueError):
            indexnow.parse_sitemap(b"<foo></foo>")


class TestLastmod(unittest.TestCase):
    def test_offset_and_z(self):
        a = indexnow.parse_lastmod("2026-07-20T10:00:00+05:30")
        b = indexnow.parse_lastmod("2026-07-20T10:00:00+00:00")
        self.assertLess(a, b)  # +05:30 is an earlier instant than +00:00
        self.assertEqual(indexnow.parse_lastmod("2026-07-20T04:30:00Z"), a)

    def test_missing_is_zero(self):
        self.assertEqual(indexnow.parse_lastmod(None), 0.0)
        self.assertEqual(indexnow.parse_lastmod(""), 0.0)
        self.assertEqual(indexnow.parse_lastmod("not-a-date"), 0.0)


class TestHygiene(unittest.TestCase):
    def setUp(self):
        self.ok = indexnow.build_hygiene("pharmeasy.in", [
            "/cart/", "/account/", "/offers/", "/search/all",
            "/prescription-medicine/*/custom-medicine", "/pe-care/",
        ])

    def test_allows_apex_and_normal(self):
        self.assertTrue(self.ok("https://pharmeasy.in"))
        self.assertTrue(self.ok("https://pharmeasy.in/online-medicine-order/foo-123"))
        self.assertTrue(self.ok("https://pharmeasy.in/prescription-medicine/foo-123"))

    def test_blocks_disallowed(self):
        self.assertFalse(self.ok("https://pharmeasy.in/cart/x"))
        self.assertFalse(self.ok("https://pharmeasy.in/offers/deal"))
        self.assertFalse(self.ok("https://pharmeasy.in/search/all?q=a"))
        self.assertFalse(self.ok("https://pharmeasy.in/pe-care/anything"))

    def test_blocks_midpath_wildcard(self):
        self.assertFalse(self.ok(
            "https://pharmeasy.in/prescription-medicine/foo-123/custom-medicine"))

    def test_blocks_other_host_and_scheme(self):
        self.assertFalse(self.ok("https://evil.example.com/x"))
        self.assertFalse(self.ok("http://pharmeasy.in/foo"))
        self.assertFalse(self.ok("https://sub.pharmeasy.in/foo"))


class TestDiff(unittest.TestCase):
    def test_new_changed_removed(self):
        prev = urlmap("urlset_v1.xml")
        cur = urlmap("urlset_v2.xml")
        diff = indexnow.compute_diff(prev, cur, set(cur))
        self.assertEqual(diff.new, [
            "https://pharmeasy.in/online-medicine-order/brand-new-product-4"])
        self.assertEqual(diff.changed, [
            "https://pharmeasy.in/online-medicine-order/changing-product-2"])
        self.assertEqual(diff.removed, [
            "https://pharmeasy.in/online-medicine-order/doomed-product-3"])

    def test_shard_move_is_not_a_removal(self):
        prev = {"https://pharmeasy.in/x": "2026-01-01T00:00:00+00:00"}
        cur_seen = {}  # url left this leaf...
        cur_all = {"https://pharmeasy.in/x"}  # ...but is present in another leaf
        diff = indexnow.compute_diff(prev, cur_seen, cur_all)
        self.assertEqual(diff.removed, [])

    def test_watermark_fallback(self):
        cur = urlmap("urlset_v2.xml")
        leaf = "leaf-a"
        leaf_of = {u: leaf for u in cur}
        # Watermark set to the v1 baseline instant: only URLs newer than it fire.
        wm = {leaf: indexnow.parse_lastmod("2026-07-20T10:00:00+05:30")}
        diff = indexnow.watermark_diff(cur, wm, leaf_of)
        self.assertIn("https://pharmeasy.in/online-medicine-order/changing-product-2", diff.changed)
        self.assertIn("https://pharmeasy.in/online-medicine-order/brand-new-product-4", diff.changed)
        self.assertNotIn("https://pharmeasy.in/online-medicine-order/stable-product-1", diff.changed)


class TestGuards(unittest.TestCase):
    def test_circuit_breaker_uses_min_floor(self):
        # Small tier: floor of 5000 dominates.
        self.assertFalse(indexnow.circuit_breaker_tripped(4999, 9000, LIMITS))
        self.assertTrue(indexnow.circuit_breaker_tripped(5001, 9000, LIMITS))

    def test_circuit_breaker_uses_fraction(self):
        # Large tier: 25% of 250k = 62500 dominates the 5000 floor.
        self.assertFalse(indexnow.circuit_breaker_tripped(62500, 250000, LIMITS))
        self.assertTrue(indexnow.circuit_breaker_tripped(62501, 250000, LIMITS))

    def test_per_run_cap_takes_most_recent(self):
        lastmod = {
            "u-old": "2026-01-01T00:00:00+00:00",
            "u-mid": "2026-06-01T00:00:00+00:00",
            "u-new": "2026-08-01T00:00:00+00:00",
        }
        to_submit, deferred = indexnow.select_for_submission(
            ["u-old", "u-new", "u-mid"], lastmod, cap=2)
        self.assertEqual(to_submit, ["u-new", "u-mid"])
        self.assertEqual(deferred, ["u-old"])

    def test_debounce_skips_recent(self):
        import time
        now = time.time()
        ledger = {"recent": now - 3600, "stale": now - 48 * 3600}
        keep, skipped = indexnow.apply_debounce(["recent", "stale", "fresh"], ledger, hours=24)
        self.assertEqual(keep, ["stale", "fresh"])
        self.assertEqual(skipped, 1)


class TestStateAndCache(unittest.TestCase):
    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state", "sitemaps.json")
            s = indexnow.State(path)
            self.assertFalse(s.seen("leaf-1"))
            s.update_leaf("leaf-1", '"etag123"', None, 10,
                          "2026-08-01T00:00:00+05:30", "ok")
            s.save({"tier": "fast"})
            s2 = indexnow.State(path)
            self.assertTrue(s2.seen("leaf-1"))
            self.assertEqual(s2.etag("leaf-1"), '"etag123"')
            self.assertGreater(s2.watermark("leaf-1"), 0.0)

    def test_cache_leaf_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            c = indexnow.Cache(d)
            leaf = "https://pharmeasy.in/sitemaps/x.xml"
            self.assertFalse(c.has_leaf(leaf))
            data = {"https://pharmeasy.in/a": "2026-08-01T00:00:00+05:30",
                    "https://pharmeasy.in/b": ""}
            c.save_leaf(leaf, data)
            self.assertTrue(c.has_leaf(leaf))
            self.assertEqual(c.load_leaf(leaf), data)

    def test_debounce_prunes_stale(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            c = indexnow.Cache(d)
            now = time.time()
            c.save_debounce({"keep": now, "drop": now - 100 * 3600}, keep_hours=48)
            loaded = c.load_debounce()
            self.assertIn("keep", loaded)
            self.assertNotIn("drop", loaded)


class TestBuildGlobals(unittest.TestCase):
    """First-seen leaves must seed silently; only previously-seen leaves diff."""

    def _crawl(self, cur_maps, etags=None):
        return indexnow.Crawl(
            cur_leaf_maps=cur_maps, etags=etags or {}, last_modified={},
            parsed=list(cur_maps), skipped=[], errored=[], warnings=[])

    def test_first_seen_leaf_produces_no_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            state = indexnow.State(os.path.join(d, "s.json"))
            cache = indexnow.Cache(d)
            cur = {"leaf-new": urlmap("urlset_v2.xml")}
            crawl = self._crawl(cur)
            prev_global, cur_seen, cur_all, leaf_of, first_seen = \
                indexnow.build_globals(crawl, state, cache)
            self.assertEqual(first_seen, ["leaf-new"])
            self.assertEqual(cur_seen, {})  # nothing to diff on a first-seen leaf
            diff = indexnow.compute_diff(prev_global, cur_seen, cur_all)
            self.assertEqual(diff.all_candidates(), [])

    def test_seen_leaf_diffs_against_cache(self):
        with tempfile.TemporaryDirectory() as d:
            state = indexnow.State(os.path.join(d, "s.json"))
            cache = indexnow.Cache(d)
            leaf = "leaf-1"
            # Seed: leaf known to state, v1 URLs in cache.
            state.update_leaf(leaf, '"e1"', None, 4, "2026-07-20T10:00:00+05:30", "ok")
            cache.save_leaf(leaf, urlmap("urlset_v1.xml"))
            crawl = self._crawl({leaf: urlmap("urlset_v2.xml")})
            prev_global, cur_seen, cur_all, leaf_of, first_seen = \
                indexnow.build_globals(crawl, state, cache)
            self.assertEqual(first_seen, [])
            diff = indexnow.compute_diff(prev_global, cur_seen, cur_all)
            self.assertEqual(len(diff.new), 1)
            self.assertEqual(len(diff.changed), 1)
            self.assertEqual(len(diff.removed), 1)


class TestRunlog(unittest.TestCase):
    def test_status_derivation(self):
        self.assertEqual(indexnow._run_status({"status": "ok"}), "ok")
        self.assertEqual(indexnow._run_status({"aborted": True}), "aborted")
        self.assertEqual(indexnow._run_status({"mode": "seed"}), "seed")
        self.assertEqual(indexnow._run_status({"errored_leaves": ["x"]}), "warn")
        self.assertEqual(indexnow._run_status({"mode": "normal"}), "ok")

    def test_render_runlog_newest_first_with_banner(self):
        with tempfile.TemporaryDirectory() as d:
            runs = os.path.join(d, "runs.jsonl")
            out = os.path.join(d, "RUNLOG.md")
            with open(runs, "w", encoding="utf-8") as f:
                f.write(json.dumps({"at": "2026-08-29T16:39:36+00:00", "tier": "full",
                                    "mode": "seed", "status": "seed", "candidates": 0,
                                    "submitted": 0}) + "\n")
                f.write(json.dumps({"at": "2026-08-29T17:20:10+00:00", "tier": "fast",
                                    "mode": "normal", "status": "ok", "candidates": 5,
                                    "submitted": 5, "deferred": 0, "warnings": 0}) + "\n")
            indexnow.render_runlog(runs, out)
            text = open(out, encoding="utf-8").read()
            self.assertIn("# IndexNow run log", text)
            self.assertIn("Last run: 2026-08-29 17:20 UTC", text)  # banner = newest
            body = text.split("|----")[1]
            # newest row must appear before the older seed row
            self.assertLess(body.index("17:20"), body.index("16:39"))
            self.assertIn("✅ ok", text)
            self.assertIn("🌱 seed", text)

    def test_render_runlog_rolling_window(self):
        with tempfile.TemporaryDirectory() as d:
            runs = os.path.join(d, "runs.jsonl")
            out = os.path.join(d, "RUNLOG.md")
            with open(runs, "w", encoding="utf-8") as f:
                for i in range(10):
                    f.write(json.dumps({"at": f"2026-08-29T10:{i:02d}:00+00:00",
                                        "tier": "fast", "mode": "normal", "status": "ok",
                                        "candidates": i, "submitted": i}) + "\n")
            indexnow.render_runlog(runs, out, window=3)
            text = open(out, encoding="utf-8").read()
            self.assertIn("Most recent 3 run(s)", text)
            self.assertIn("10:09", text)      # newest kept
            self.assertNotIn("10:06", text)   # outside the window

    def test_render_runlog_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "RUNLOG.md")
            indexnow.render_runlog(os.path.join(d, "nope.jsonl"), out)
            self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
