# ign-elevation-cache-proxy

Independently deployable, loopback-only, SQLite-backed caching proxy for
Earth Platform's IGN (Institut national de l'information geographique et
forestiere) RGE ALTI elevation WMS -- MINECRAFT-578 / MINECRAFT-571.

Pinned source: https://github.com/pirut/earth-platform, tag `combined-73`
(= release "Earth Platform 0.1.0-alpha.73", commit
`88b13c33954550c6ac660dceb743a4288a9aafac`). Endpoint inventory below was
read from that exact tag's
`geography/src/main/java/io/earthplatform/geography/globe/IgnElevation.java`
and `RangeFiles.java`, not `main`.

## Run

```
python3 -m unittest test_proxy -v     # 13 tests, local mock upstream only
python3 proxy.py                      # serves on 127.0.0.1:8075 by default
```

## Contract

- Loopback-only bind (`127.0.0.1`, hardcoded in `run_server`).
- Fixed, allowlisted upstream: `https://data.geopf.fr/wms-r/wms` by default
  (unchanged from the mod's own `IgnElevation.IGN_WMS`). Override via
  `IGN_CACHE_UPSTREAM`; the default is untouched unless a caller sets it.
- **Different native shape from a tile proxy, by design**: IGN has no
  quantized tile path on its own side. `IgnElevation.pieces()` builds one WMS
  `GetMap` query per source tile from a computed lat/lon block (`BBOX`,
  `WIDTH`, `HEIGHT` vary continuously). This proxy's cache key is the
  normalized `GetMap` query string itself -- exactly as specific as the
  mod's own request, and deterministic per (zoom, x, y) because
  `IgnElevation.block()` derives it deterministically. Only the exact
  `SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES
  &CRS=EPSG:4326&FORMAT=image/x-bil;bits=32` shape with numeric, bounded
  `BBOX`/`WIDTH`/`HEIGHT` is accepted -- this is a GetMap-only relay, not a
  general WMS reverse proxy; any other layer, request type, or malformed
  query is refused (400) before ever reaching upstream.
- SQLite grid cache, keyed by the normalized query string, restart-persistent.
- **Cache lifetime: no time-based expiration (TTL -1)**, per John's
  2026-11-24 Earth-Platform-wide decision -- not unbounded: still subject to
  the byte cap/LRU eviction below, and `DELETE /?<query>` explicitly
  invalidates one entry. There is no dedicated upstream health-check
  resource for this source -- ordinary GetMap requests are the only upstream
  traffic, so the separate TTL-0-health-check rule has nothing to apply to
  here.
- Range requests served as 206 slices of the cached object; a stable
  SHA-256 ETag supports `If-None-Match` -> 304. Matches the mod's own
  `RangeFiles.fetch()`, which always requests `Range: bytes=0-4194303` and
  accepts both 200 and 206 as success -- live-confirmed the real service
  answers 206 to that Range on a GetMap response.
- A WMS `ServiceExceptionReport` (bad/rejected query), any non-(200/206)
  status, or a body whose length isn't exactly `WIDTH*HEIGHT*4` bytes is
  never cached and is retried as a transient failure (3 retries, backoff),
  mirroring the mod's own `IgnElevation.decode()`, which rejects a
  wrong-sized BIL body before ever decoding it.
- A `429`/`503` (IGN's own documented ~1 request/second limit -- live-
  confirmed via `x-ratelimit-limit-second: 1`) is treated as a distinct
  backoff-and-retry condition, not a hard failure surfaced to the mod.
- Concurrent cache misses for the same query single-flight to exactly one
  upstream request.
- Bounded storage: `max_bytes`, LRU eviction down to 90% on overflow.

## Live verification (read-only, 2026-11-24, no caching/mutation)

- A valid `GetMap` request against the real `data.geopf.fr/wms-r/wms`
  answered `200` with `Content-Type: image/x-bil;bits=32`, and the same
  request with `Range: bytes=0-4194303` answered `206` -- Range is genuinely
  honored upstream, not just assumed.
- An invalid `LAYERS=BOGUS` request answered `400` with a
  `ServiceExceptionReport` XML body -- confirms the malformed-response case
  this proxy refuses to cache.
- Response headers also showed `x-ratelimit-limit-second: 1`, informing the
  429/503 backoff behavior above.

## Terms / attribution

IGN's RGE ALTI is published under France's Licence Ouverte / Etalab Open
Licence 2.0, which permits reproduction and caching with attribution. The
mod already carries the required attribution string
(`IgnElevation.ATTRIBUTION`); this proxy does not touch or strip it. No
terms blocker found for running this cache.

## Status

Both GitHub destinations exist (`brooswit-intel/ign-elevation-cache-proxy`
and the pinned `brooswit-minecraft/earth-platform` fork for later wiring).
The repo-only Write invitation for this agent's GitHub account (`booswrit`)
was accepted and verified (`gh repo view ... --json viewerPermission` ->
`WRITE`) before this tree was pushed.
