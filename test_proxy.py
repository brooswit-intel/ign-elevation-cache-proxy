import http.client
import os
import struct
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import proxy as ign

WIDTH, HEIGHT = 4, 4
GOOD_BODY = struct.pack(f"<{WIDTH * HEIGHT}f", *[float(i) for i in range(WIDTH * HEIGHT)])

GOOD_QUERY = (
    "SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
    "&STYLES=&CRS=EPSG:4326&BBOX=48.8,2.3,48.9,2.4&WIDTH=4&HEIGHT=4&FORMAT=image/x-bil;bits=32"
)


class FakeUpstream(BaseHTTPRequestHandler):
    """Stands in for data.geopf.fr/wms-r/wms: serves one real grid, one
    WMS exception (bad layer), one rate-limited-then-recovers grid, and a
    size-mismatched "success" for anything else."""

    hits = []
    rate_limit_countdown = {"n": 0}

    def do_GET(self):
        FakeUpstream.hits.append(self.path)
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        bbox = query.get("BBOX", [""])[0]
        if bbox == "48.8,2.3,48.9,2.4":
            self._bil(GOOD_BODY)
        elif bbox == "0.0,0.0,1.0,1.0":  # rate-limited a couple of times, then succeeds
            if FakeUpstream.rate_limit_countdown["n"] > 0:
                FakeUpstream.rate_limit_countdown["n"] -= 1
                body = b"rate limited"
                self.send_response(429)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._bil(GOOD_BODY)
        elif bbox == "9.0,9.0,10.0,10.0":  # WMS rejects the layer
            body = b'<?xml version="1.0"?><ServiceExceptionReport><ServiceException code="LayerNotDefined">Layer unknown</ServiceException></ServiceExceptionReport>'
            self.send_response(400)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif bbox == "30.0,30.0,31.0,31.0":  # ignores Range entirely, answers with far more than requested
            body = GOOD_BODY + b"\x00" * (1024 * 1024)
            self.send_response(200)
            self.send_header("Content-Type", "image/x-bil;bits=32")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif bbox == "40.0,40.0,41.0,41.0":
            # 206 with the right length but a Content-Range offset that proves it is not
            # byte 0 -- the exact equal-length wrong-offset case from MINECRAFT-571 55866.
            self.send_response(206)
            self.send_header("Content-Type", "image/x-bil;bits=32")
            self.send_header("Content-Length", str(len(GOOD_BODY)))
            self.send_header("Content-Range", f"bytes 20-{20 + len(GOOD_BODY) - 1}/10000")
            self.end_headers()
            self.wfile.write(GOOD_BODY)
        else:  # "success" with the wrong number of bytes for its WIDTH/HEIGHT
            body = b"\x00" * 3
            self.send_response(200)
            self.send_header("Content-Type", "image/x-bil;bits=32")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _bil(self, body: bytes):
        self.send_response(206)
        self.send_header("Content-Type", "image/x-bil;bits=32")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class ProxyTest(unittest.TestCase):
    def setUp(self):
        FakeUpstream.hits = []
        FakeUpstream.rate_limit_countdown["n"] = 0
        self.upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.upstream_port = self.upstream_server.server_address[1]
        threading.Thread(target=self.upstream_server.serve_forever, daemon=True).start()

        self.db_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.db_dir, "ign.sqlite3")
        self.server, self.proxy = ign.run_server(
            self.db_path, 0, upstream_base=f"http://127.0.0.1:{self.upstream_port}/wms-r/wms"
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.proxy.close()
        self.upstream_server.shutdown()
        self.upstream_server.server_close()

    def _get(self, query, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("GET", "/?" + query, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def _get_path(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_query_validation_enforces_exact_getmap_shape(self):
        self.assertIsNotNone(ign.normalize_query(GOOD_QUERY))
        self.assertIsNone(ign.normalize_query(GOOD_QUERY.replace("LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES", "LAYERS=SOME.OTHER.LAYER")))
        self.assertIsNone(ign.normalize_query(GOOD_QUERY.replace("WIDTH=4", "WIDTH=100000")))
        self.assertIsNone(ign.normalize_query(GOOD_QUERY.replace("REQUEST=GetMap", "REQUEST=GetFeatureInfo")))
        self.assertIsNone(ign.normalize_query("not a wms query at all"))

    def test_real_grid_is_cached_and_served(self):
        resp, body = self._get(GOOD_QUERY)
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, GOOD_BODY)
        self.assertEqual(len(FakeUpstream.hits), 1)

        resp2, body2 = self._get(GOOD_QUERY)
        self.assertEqual(resp2.status, 200)
        self.assertEqual(body2, GOOD_BODY)
        self.assertEqual(len(FakeUpstream.hits), 1, "cache hit must not re-fetch upstream")
        self.assertEqual(self.proxy.stats.as_dict()["cache_hits"], 1)

    def test_wms_service_exception_is_never_cached(self):
        bad_query = GOOD_QUERY.replace("BBOX=48.8,2.3,48.9,2.4", "BBOX=9,9,10,10")
        resp, _ = self._get(bad_query)
        self.assertEqual(resp.status, 502)
        resp2, _ = self._get(bad_query)
        self.assertEqual(resp2.status, 502)
        self.assertGreaterEqual(len(FakeUpstream.hits), 2, "a WMS ServiceExceptionReport must be retried, never cached")

    def test_size_mismatched_success_is_never_cached(self):
        other_query = GOOD_QUERY.replace("BBOX=48.8,2.3,48.9,2.4", "BBOX=20,20,21,21")
        resp, _ = self._get(other_query)
        self.assertEqual(resp.status, 502)
        self.assertIsNone(self.proxy.cache.get(ign.normalize_query(other_query)))

    def test_origin_ignoring_range_is_rejected_not_downloaded_unbounded(self):
        oversized_query = GOOD_QUERY.replace("BBOX=48.8,2.3,48.9,2.4", "BBOX=30,30,31,31")
        resp, _ = self._get(oversized_query)
        self.assertEqual(resp.status, 502)
        self.assertIsNone(self.proxy.cache.get(ign.normalize_query(oversized_query)))

    def test_206_with_wrong_offset_content_range_is_rejected(self):
        wrong_offset_query = GOOD_QUERY.replace("BBOX=48.8,2.3,48.9,2.4", "BBOX=40,40,41,41")
        resp, _ = self._get(wrong_offset_query)
        self.assertEqual(resp.status, 502, "a 206 whose Content-Range proves a non-zero offset must never be trusted as this grid's own bytes")
        self.assertIsNone(self.proxy.cache.get(ign.normalize_query(wrong_offset_query)))

    def test_rate_limit_is_retried_not_treated_as_failure(self):
        FakeUpstream.rate_limit_countdown["n"] = 2
        rl_query = GOOD_QUERY.replace("BBOX=48.8,2.3,48.9,2.4", "BBOX=0,0,1,1")
        resp, body = self._get(rl_query)
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, GOOD_BODY)
        self.assertGreaterEqual(len(FakeUpstream.hits), 3, "429 must be retried until it succeeds, not surfaced as an upstream failure")

    def test_range_request_served_as_206_slice(self):
        self._get(GOOD_QUERY)
        resp, body = self._get(GOOD_QUERY, {"Range": "bytes=0-7"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(body, GOOD_BODY[:8])
        self.assertEqual(resp.getheader("Content-Range"), f"bytes 0-7/{len(GOOD_BODY)}")

    def test_conditional_request_answers_304(self):
        resp, _ = self._get(GOOD_QUERY)
        etag = resp.getheader("ETag")
        resp2, body2 = self._get(GOOD_QUERY, {"If-None-Match": etag})
        self.assertEqual(resp2.status, 304)
        self.assertEqual(body2, b"")

    def test_concurrent_miss_deduplication_single_flight(self):
        barrier = threading.Barrier(5)
        results = []

        def worker():
            barrier.wait()
            resp, _ = self._get(GOOD_QUERY)
            results.append(resp.status)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [200] * 5)
        self.assertEqual(len(FakeUpstream.hits), 1, "5 concurrent misses for the same grid must cause exactly 1 upstream fetch")

    def test_restart_persists_cache_with_no_time_expiry(self):
        self._get(GOOD_QUERY)
        self.proxy.close()
        self.server.shutdown()
        self.server.server_close()

        with mock.patch("time.time", return_value=time.time() + 10 * 365 * 24 * 3600):
            server2, proxy2 = ign.run_server(self.db_path, 0, upstream_base=f"http://127.0.0.1:{self.upstream_port}/wms-r/wms")
            try:
                cached = proxy2.cache.get(ign.normalize_query(GOOD_QUERY))
                self.assertIsNotNone(cached)
            finally:
                proxy2.close()
                server2.server_close()  # never served; shutdown() would hang waiting on serve_forever's own event
        self.assertEqual(len(FakeUpstream.hits), 1)

    def test_explicit_invalidation_forces_refetch(self):
        self._get(GOOD_QUERY)
        self.assertEqual(len(FakeUpstream.hits), 1)

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("DELETE", "/?" + GOOD_QUERY)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)

        self._get(GOOD_QUERY)
        self.assertEqual(len(FakeUpstream.hits), 2, "invalidated entry must be re-fetched on next request")

    def test_bytes_cap_evicts_lru(self):
        small_db = os.path.join(self.db_dir, "small.sqlite3")
        max_bytes = int(len(GOOD_BODY) * 1.5)
        server, proxy = ign.run_server(
            small_db, 0, upstream_base=f"http://127.0.0.1:{self.upstream_port}/wms-r/wms", max_bytes=max_bytes
        )
        try:
            proxy.cache.put("q1", GOOD_BODY)
            proxy.cache.evict_to_fit(proxy.stats)
            proxy.cache.put("q2", GOOD_BODY)
            proxy.cache.evict_to_fit(proxy.stats)
            self.assertIsNone(proxy.cache.get("q1"), "oldest entry must be evicted once over the byte cap")
            self.assertIsNotNone(proxy.cache.get("q2"))
        finally:
            proxy.close()
            server.server_close()  # never served; shutdown() would hang waiting on serve_forever's own event

    def test_default_upstream_unchanged_when_unset(self):
        proxy = ign.IgnCacheProxy(os.path.join(self.db_dir, "defaults.sqlite3"))
        try:
            self.assertEqual(proxy.upstream_base, ign.DEFAULT_UPSTREAM)
        finally:
            proxy.close()

    def test_env_override_changes_upstream(self):
        with mock.patch.dict(os.environ, {"IGN_CACHE_UPSTREAM": "http://example.invalid/wms"}):
            proxy = ign.IgnCacheProxy(os.path.join(self.db_dir, "override.sqlite3"))
            try:
                self.assertEqual(proxy.upstream_base, "http://example.invalid/wms")
            finally:
                proxy.close()

    def test_health_endpoint_is_fresh_and_bypasses_the_grid_cache(self):
        with mock.patch.object(ign.IgnCacheProxy, "HEALTH_PROBE_QUERY", GOOD_QUERY):
            # Prime the ordinary cache so a stale hit there can't masquerade as a live probe.
            self._get(GOOD_QUERY)
            hits_before = len(FakeUpstream.hits)

            resp, body = self._get_path("/__health")
            self.assertEqual(resp.status, 200)
            self.assertEqual(body, b"")
            self.assertEqual(len(FakeUpstream.hits), hits_before + 1, "/__health must always hit upstream fresh, never the grid cache")

            resp2, _ = self._get_path("/__health")
            self.assertEqual(resp2.status, 200)
            self.assertEqual(len(FakeUpstream.hits), hits_before + 2, "a second /__health call must probe again, not reuse the first result")
            self.assertEqual(self.proxy.stats.as_dict()["probe_requests"], 2)

    def test_health_endpoint_reports_failure_without_poisoning_grid_cache(self):
        bad_probe_query = GOOD_QUERY.replace("BBOX=48.8,2.3,48.9,2.4", "BBOX=9,9,10,10")
        with mock.patch.object(ign.IgnCacheProxy, "HEALTH_PROBE_QUERY", bad_probe_query):
            resp, _ = self._get_path("/__health")
            self.assertEqual(resp.status, 502)
            self.assertEqual(self.proxy.stats.as_dict()["probe_failures"], 1)
        self.assertIsNone(self.proxy.cache.get(ign.normalize_query(bad_probe_query)))

    def test_head_on_ordinary_grid_goes_through_the_normal_cache(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("HEAD", "/?" + GOOD_QUERY)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"")  # HEAD never carries a body
        self.assertEqual(resp.getheader("Content-Length"), str(len(GOOD_BODY)))
        self.assertEqual(len(FakeUpstream.hits), 1, "a HEAD on a grid must still go through (and populate) the ordinary cache")


if __name__ == "__main__":
    unittest.main()
