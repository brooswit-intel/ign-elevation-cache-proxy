"""Loopback-only caching proxy for IGN's (Institut national de l'information
geographique et forestiere) Geoplateforme RGE ALTI WMS elevation service.

Upstream contract (inventoried from Earth Platform 0.1.0-alpha.73 / tag
combined-73, geography/src/main/java/io/earthplatform/geography/globe/IgnElevation.java,
corroborated live against the real endpoint on 2026-11-24):
  service: https://data.geopf.fr/wms-r/wms
  request shape (one WMS GetMap per source tile, built by IgnElevation.pieces()):
    ?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES
     &STYLES=&CRS=EPSG:4326&BBOX=<south,west,north,east>&WIDTH=<n>&HEIGHT=<n>
     &FORMAT=image/x-bil;bits=32
  BBOX/WIDTH/HEIGHT vary continuously per tile (computed from IgnElevation.Block,
  not a fixed z/x/y path); unlike GSI, there is no quantized tile-path cache key
  on IGN's own side. This proxy's cache key is therefore the normalized query
  string itself, which is exactly as specific as the mod's own request, and
  deterministic per (zoom, x, y) because IgnElevation.block() derives it
  deterministically. The mod's RangeFiles.fetch() always sends
  "Range: bytes=0-4194303"; live probe confirmed the real service answers 206
  to that Range on a GetMap response, and 400 ServiceExceptionReport (XML, not
  binary BIL) on a malformed/rejected request -- never a silent empty success.
  Live probe also showed the service rate-limits to roughly 1 req/s
  ("x-ratelimit-limit-second: 1"); this proxy's own upstream concurrency
  defaults low and backs off on 429/503 for that reason, independent of the
  mod's own retry loop on the client side of this proxy.

Default upstream is exactly the mod's existing default. IGN_CACHE_UPSTREAM
overrides it (the override point the ticket asks for); unset, behavior is
unchanged. Only LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES, the fixed
CRS/FORMAT/VERSION/REQUEST/SERVICE combination actually used by the mod, and
numeric BBOX/WIDTH/HEIGHT are forwarded -- no arbitrary WMS layer/query
passthrough (this is a GetMap-only relay, not a general WMS reverse proxy).

Cache lifetime (per John's Earth-Platform-wide decision, 2026-11-24): a
validated data response has no time-based expiration (TTL -1, still subject
to the byte cap/LRU eviction below and explicit invalidation). A WMS
ServiceExceptionReport, any non-(200/206) status, or a body whose length
doesn't match WIDTH*HEIGHT*4 bytes exactly is never cached. There is no
dedicated upstream health-check resource for this source -- ordinary GetMap
requests are the only upstream traffic, so John's separate TTL-0-health-check
rule has no distinct resource to apply to here.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_UPSTREAM = "https://data.geopf.fr/wms-r/wms"
MAX_BYTES = 4 * 1024 * 1024  # matches the mod's own RangeFiles MAX_BYTES bound
LAYER = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
REQUIRED_PARAMS = {
    "SERVICE": "WMS",
    "VERSION": "1.3.0",
    "REQUEST": "GetMap",
    "LAYERS": LAYER,
    "STYLES": "",
    "CRS": "EPSG:4326",
    "FORMAT": "image/x-bil;bits=32",
}


def normalize_query(raw_query: str) -> str | None:
    """Validates the request is exactly the GetMap shape IgnElevation.pieces()
    builds and nothing else (no arbitrary WMS layer/query forwarding); returns
    a canonical query string (stable key ordering) to use as both the cache
    key and the upstream query, or None to refuse.

    WIDTH/HEIGHT are bounded (IgnElevation never asks for more than a few
    hundred pixels a side; a four-figure request is already far outside any
    real tile and is refused rather than relayed)."""
    params = urllib.parse.parse_qs(raw_query, keep_blank_values=True, strict_parsing=False)
    flat = {k: v[0] for k, v in params.items() if len(v) == 1}
    if len(flat) != len(params):
        return None  # a repeated key is not a shape this mod ever sends
    for key, expected in REQUIRED_PARAMS.items():
        if flat.get(key) != expected:
            return None
    bbox = flat.get("BBOX", "")
    parts = bbox.split(",")
    if len(parts) != 4:
        return None
    try:
        south, west, north, east = (float(p) for p in parts)
    except ValueError:
        return None
    if not (south < north and west < east):
        return None
    try:
        width, height = int(flat.get("WIDTH", "")), int(flat.get("HEIGHT", ""))
    except ValueError:
        return None
    if not (1 <= width <= 2048 and 1 <= height <= 2048):
        return None
    ordered = {**REQUIRED_PARAMS, "BBOX": f"{south!r},{west!r},{north!r},{east!r}", "WIDTH": str(width), "HEIGHT": str(height)}
    return urllib.parse.urlencode(ordered)


@dataclass
class Stats:
    upstream_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    not_modified: int = 0
    bytes_downloaded: int = 0
    evictions: int = 0
    rate_limited_retries: int = 0
    probe_requests: int = 0
    probe_failures: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def as_dict(self) -> dict:
        with self.lock:
            return {
                "upstream_requests": self.upstream_requests,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "not_modified": self.not_modified,
                "bytes_downloaded": self.bytes_downloaded,
                "evictions": self.evictions,
                "rate_limited_retries": self.rate_limited_retries,
                "probe_requests": self.probe_requests,
                "probe_failures": self.probe_failures,
            }


class GridCache:
    """SQLite-backed, restart-persistent, bounded cache of decoded BIL grids,
    keyed by the normalized GetMap query string. No time-based expiration
    (TTL -1): valid until explicitly invalidated or evicted for space.

    Never stores a response unless it is a genuine, fully-transferred,
    correctly-sized BIL grid. A WMS error, a timeout, a 5xx, a rate-limit
    response, or a truncated/mis-sized body is never written.
    """

    def __init__(self, db_path: str, max_bytes: int):
        self.max_bytes = max_bytes
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS grids (
                 query TEXT PRIMARY KEY,
                 etag TEXT NOT NULL,
                 body BLOB NOT NULL,
                 size INTEGER NOT NULL,
                 created_at REAL NOT NULL,
                 last_used_at REAL NOT NULL
               )"""
        )
        self._conn.commit()
        self._db_lock = threading.Lock()

    def get(self, query: str):
        with self._db_lock:
            row = self._conn.execute("SELECT etag, body FROM grids WHERE query = ?", (query,)).fetchone()
            if row is None:
                return None
            self._conn.execute("UPDATE grids SET last_used_at = ? WHERE query = ?", (time.time(), query))
            self._conn.commit()
        return row[0], row[1]

    def put(self, query: str, body: bytes) -> str:
        etag = hashlib.sha256(body).hexdigest()
        now = time.time()
        with self._db_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO grids (query, etag, body, size, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (query, etag, body, len(body), now, now),
            )
            self._conn.commit()
        return etag

    def invalidate(self, query: str) -> bool:
        with self._db_lock:
            cur = self._conn.execute("DELETE FROM grids WHERE query = ?", (query,))
            self._conn.commit()
            return cur.rowcount > 0

    def evict_to_fit(self, stats: Stats) -> None:
        if self.max_bytes <= 0:
            return
        target = self.max_bytes * 9 // 10
        with self._db_lock:
            total = self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM grids").fetchone()[0]
            if total <= self.max_bytes:
                return
            rows = self._conn.execute("SELECT query, size FROM grids ORDER BY last_used_at ASC").fetchall()
            removed = 0
            for query, size in rows:
                if total - removed <= target:
                    break
                self._conn.execute("DELETE FROM grids WHERE query = ?", (query,))
                removed += size
                with stats.lock:
                    stats.evictions += 1
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class UpstreamError(Exception):
    pass


def _looks_like_wms_exception(body: bytes) -> bool:
    head = body.lstrip()[:200]
    return head.startswith(b"<?xml") or head.startswith(b"<ServiceException")


#: generous bound for an error/non-grid body we only need to classify, not use.
_DRAIN_CAP = 4096


def fetch_upstream(upstream_base: str, query: str, width: int, height: int, timeout: float = 15.0, retries: int = 4):
    """GET the GetMap query with the same Range window the mod itself sends.
    Treats 429/503 (IGN's documented ~1 req/s limit) as a backoff-and-retry
    condition distinct from a hard failure; any other non-(200/206), a WMS
    ServiceExceptionReport body, or a body whose length isn't exactly
    width*height*4 bytes raises UpstreamError without being cached.

    Status is checked BEFORE any body is read, and every read is capped --
    `expected_bytes + 1` for a claimed success (IGN's own exact size is
    always known up front from width*height, so this is a tight bound, not
    a generous one), `_DRAIN_CAP` otherwise -- never an unbounded
    `resp.read()`. An origin that ignores Range and tries to answer with
    something far larger than the exact grid size is read only up to that
    cap, found oversized by the length check below, and rejected -- it is
    never allowed to force an unbounded download before that rejection
    happens."""
    parsed = urllib.parse.urlsplit(upstream_base)
    full_path = parsed.path + "?" + query
    expected_bytes = width * height * 4
    last_exc: Exception | None = None
    slowed = 0
    for attempt in range(retries):
        if attempt:
            time.sleep(0.25 * (2 ** attempt))
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.netloc, timeout=timeout)
        try:
            conn.request(
                "GET",
                full_path,
                headers={"User-Agent": "earth-platform/ign-rgealti-v1", "Range": f"bytes=0-{MAX_BYTES - 1}"},
            )
            resp = conn.getresponse()
            if resp.status in (429, 503):
                slowed += 1
                last_exc = UpstreamError(f"HTTP {resp.status} (rate limited) for {full_path}")
                resp.read(_DRAIN_CAP)
                continue
            if resp.status not in (200, 206):
                last_exc = UpstreamError(f"HTTP {resp.status} for {full_path}")
                resp.read(_DRAIN_CAP)
                continue
            body = resp.read(expected_bytes + 1)
            if len(body) > expected_bytes:
                last_exc = UpstreamError(f"response exceeded the expected {expected_bytes}-byte grid for {full_path} (origin may be ignoring Range)")
                continue
            declared = resp.getheader("Content-Length")
            if declared is not None and int(declared) != len(body):
                last_exc = UpstreamError(f"truncated transfer for {full_path}: declared {declared}, got {len(body)}")
                continue
            if _looks_like_wms_exception(body):
                last_exc = UpstreamError(f"WMS ServiceExceptionReport (HTTP {resp.status}) for {full_path}: {body[:200]!r}")
                continue
            if len(body) != expected_bytes:
                last_exc = UpstreamError(f"IGN answered {len(body)} bytes, not {width}x{height} floats ({expected_bytes}) for {full_path}")
                continue
            return body
        except (OSError, http.client.HTTPException) as net_err:
            last_exc = net_err
            continue
        finally:
            conn.close()
    raise UpstreamError(f"upstream fetch failed after {retries} attempts: {last_exc}")


class _Flight(threading.Event):
    result = None
    error: Exception | None = None


class _SingleFlight:
    def __init__(self):
        self._lock = threading.Lock()
        self._inflight: dict[str, _Flight] = {}

    def run(self, key: str, loader):
        with self._lock:
            flight = self._inflight.get(key)
            is_leader = flight is None
            if is_leader:
                flight = _Flight()
                self._inflight[key] = flight
        if is_leader:
            try:
                flight.result = loader()
            except Exception as exc:  # noqa: BLE001 - propagated to every waiter
                flight.error = exc
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                flight.set()
        else:
            flight.wait()
        if flight.error is not None:
            raise flight.error
        return flight.result


class IgnCacheProxy:
    def __init__(self, db_path: str, upstream_base: str | None = None, max_bytes: int = 256 * 1024 * 1024):
        self.upstream_base = upstream_base or os.environ.get("IGN_CACHE_UPSTREAM", DEFAULT_UPSTREAM)
        self.cache = GridCache(db_path, max_bytes)
        self.stats = Stats()
        self._single_flight = _SingleFlight()

    def serve_grid(self, raw_query: str, if_none_match: str | None, range_header: str | None):
        query = normalize_query(raw_query)
        if query is None:
            return 400, {}, b""
        width = int(urllib.parse.parse_qs(query)["WIDTH"][0])
        height = int(urllib.parse.parse_qs(query)["HEIGHT"][0])

        cached = self.cache.get(query)
        if cached is None:
            def load():
                body = fetch_upstream(self.upstream_base, query, width, height)
                with self.stats.lock:
                    self.stats.upstream_requests += 1
                    self.stats.bytes_downloaded += len(body)
                etag = self.cache.put(query, body)
                self.cache.evict_to_fit(self.stats)
                return etag, body

            with self.stats.lock:
                self.stats.cache_misses += 1
            try:
                etag, body = self._single_flight.run(query, load)
            except UpstreamError:
                return 502, {}, b""
        else:
            with self.stats.lock:
                self.stats.cache_hits += 1
            etag, body = cached

        if if_none_match is not None and if_none_match == etag:
            with self.stats.lock:
                self.stats.not_modified += 1
            return 304, {"ETag": etag}, b""

        if range_header and range_header.startswith("bytes="):
            start_s, _, end_s = range_header[len("bytes="):].partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else len(body) - 1
            end = min(end, len(body) - 1)
            if start > end or start >= len(body):
                return 416, {"ETag": etag, "Content-Range": f"bytes */{len(body)}"}, b""
            chunk = body[start:end + 1]
            headers = {"ETag": etag, "Content-Range": f"bytes {start}-{end}/{len(body)}", "Content-Type": "image/x-bil;bits=32"}
            return 206, headers, chunk

        return 200, {"ETag": etag, "Content-Type": "image/x-bil;bits=32"}, body

    def invalidate(self, raw_query: str) -> bool:
        query = normalize_query(raw_query)
        return False if query is None else self.cache.invalidate(query)

    #: a representative, small, valid GetMap query the health probe uses -- any in-coverage
    #: bbox/width/height works; kept deliberately tiny (16x16) to make the probe cheap.
    HEALTH_PROBE_QUERY = (
        "SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
        "&STYLES=&CRS=EPSG:4326&BBOX=48.8,2.3,48.9,2.4&WIDTH=16&HEIGHT=16&FORMAT=image/x-bil;bits=32"
    )

    def health_check(self) -> tuple[int, dict]:
        """The dedicated upstream health-check endpoint (/__health): TTL 0, always a fresh
        live probe against the real upstream, never reads or writes the grid cache in either
        direction. Reflects upstream reachability only, independent of EarthStatus's separate
        plain-HEAD probe contract and HostBackoff's own Range recovery probe against an
        ordinary GetMap URI -- both of those exercise the normal cache (TTL -1) on purpose;
        this one deliberately never does."""
        query = normalize_query(self.HEALTH_PROBE_QUERY)
        width = int(urllib.parse.parse_qs(query)["WIDTH"][0])
        height = int(urllib.parse.parse_qs(query)["HEIGHT"][0])
        with self.stats.lock:
            self.stats.probe_requests += 1
        try:
            fetch_upstream(self.upstream_base, query, width, height)
            return 200, {}
        except UpstreamError:
            with self.stats.lock:
                self.stats.probe_failures += 1
            return 502, {}

    def close(self):
        self.cache.close()


def make_handler(proxy: IgnCacheProxy):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet; /__stats covers verification
            pass

        def do_GET(self):
            self._dispatch(write_body=True)

        def do_HEAD(self):
            # No method-based special-casing on the ordinary grid resource: a HEAD gets exactly
            # the headers/status a GET would, through the normal TTL -1 cache, same as GET. The
            # dedicated /__health endpoint below is the TTL-0 live-reachability probe.
            self._dispatch(write_body=False)

        def _dispatch(self, write_body):
            path, _, query = self.path.partition("?")
            if path == "/__stats":
                import json
                payload = json.dumps(proxy.stats.as_dict()).encode()
                self._respond(200, {"Content-Type": "application/json"}, payload, write_body)
                return
            if path == "/__health":
                status, headers = proxy.health_check()
                self._respond(status, headers, b"", write_body)
                return
            status, headers, body = proxy.serve_grid(query, self.headers.get("If-None-Match"), self.headers.get("Range"))
            self._respond(status, headers, body, write_body)

        def do_DELETE(self):
            _, _, query = self.path.partition("?")
            removed = proxy.invalidate(query)
            self._respond(200 if removed else 404, {}, b"", write_body=True)

        def _respond(self, status, headers, body, write_body=True):
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if write_body and body:
                self.wfile.write(body)

    return Handler


def run_server(db_path: str, port: int, upstream_base: str | None = None, max_bytes: int = 256 * 1024 * 1024):
    proxy = IgnCacheProxy(db_path, upstream_base, max_bytes)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(proxy))
    return server, proxy


if __name__ == "__main__":
    port = int(os.environ.get("IGN_CACHE_PORT", "8075"))
    db = os.environ.get("IGN_CACHE_DB", "ign-elevation-cache.sqlite3")
    server, proxy = run_server(db, port)
    print(f"ign elevation cache proxy on http://127.0.0.1:{port} -> upstream {proxy.upstream_base}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        proxy.close()
