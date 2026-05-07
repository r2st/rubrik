# CDN runbook — putting CloudFront / Fastly in front of the API

## Why

The API already emits `ETag` + `Cache-Control: max-age=60` on read endpoints
(see `api/caching.py`). Wiring a CDN at the edge means:

1. Static frontend assets (`/`, `/static/*`) are served without ever hitting
   uvicorn.
2. Repeat reads of `/api/v1/summary`, `/api/v1/meetings`, etc. by the same
   client (or by 1k different clients in a 60-second window) hit the edge,
   not the origin. Origin RPS drops by an order of magnitude on read-heavy
   workloads.
3. Geographic latency drops to whatever the user's nearest edge POP gives.

## Routing rules

| Path prefix | Cache | Notes |
|---|---|---|
| `/static/*` | Public, max-age 86400, immutable | Files are versioned by build hash; safe to cache long. |
| `/`, `/admin` | No store | Always go to origin (HTML shell varies by build). |
| `/api/health`, `/api/live`, `/api/ready` | No store | Probes must reach the actual replica. |
| `/api/v1/summary`, `/api/v1/meetings`, … (GET) | Honor origin headers, public | API already emits `Cache-Control: max-age=60` + ETag. |
| `/api/v1/admin/*` | No store, bypass | Session cookies + audited writes. |
| `POST/PUT/DELETE *` | Bypass | Never cache mutations. |

## CloudFront — `viewer-request` Lambda@Edge

Add a request-policy that strips the `Authorization` and `X-API-Key`
headers from the cache key (otherwise CloudFront treats every key holder
as a separate origin) **but** forwards them to the origin. Otherwise
identical-payload requests from different tenants would never hit the
edge cache.

```js
// viewer-request: forward auth headers, but exclude them from the cache key
const headers = event.Records[0].cf.request.headers;
delete headers['x-api-key-cache-key']; // dummy
return event;
```

## Fastly VCL equivalent

```vcl
sub vcl_recv {
  if (req.url ~ "^/api/(health|live|ready)") { return(pass); }
  if (req.url ~ "^/api/v1/admin/")            { return(pass); }
  if (req.method != "GET" && req.method != "HEAD") { return(pass); }
  unset req.http.X-API-Key;          # not part of the cache key
}
```

## Validation

Smoke checks after CDN cutover:

```bash
# 1. Static asset is served from edge.
curl -sI https://api.example.com/static/app.js | grep -i 'x-cache: hit'

# 2. /api/health hits origin (200) — never cached.
for i in 1 2 3; do
  curl -sI https://api.example.com/api/health | grep -i 'x-cache'
done
# expect three "Miss" / "Pass" results

# 3. /api/v1/summary is cached.
curl -sI -H 'X-API-Key: $KEY' https://api.example.com/api/v1/summary
# subsequent identical request:
curl -sI -H 'X-API-Key: $KEY' https://api.example.com/api/v1/summary | grep -i 'x-cache: hit'

# 4. Admin endpoints bypass.
curl -sI https://api.example.com/api/v1/admin/me | grep -i 'x-cache: pass\|x-cache: miss'
```

## Headline metrics

After cutover, watch these for two weeks before considering it stable:

| Metric | Expected |
|---|---|
| Edge cache hit ratio (read APIs) | > 70% steady state |
| Origin RPS reduction | 5-10× on read-heavy mix |
| p95 latency (cached path) | < 50 ms globally |
| Origin error rate | unchanged or lower (less load) |

If the hit ratio doesn't move, the most common causes are:
- A cookie/header is in the cache key when it shouldn't be (check Vary).
- The origin is sending `Cache-Control: no-store` for paths that should be
  cacheable (audit `api/caching.py`).
- The TTL is too short for the access pattern (raise `max-age` in routes).

## Cost note

CloudFront / Fastly bandwidth is meaningfully cheaper than uvicorn pod-hours
serving the same payload. At ~30 KB / `summary` response and 100M req/day, a
70% hit ratio saves roughly $1.5k/day in compute. Storage + edge egress for
the cached fraction adds back ~$200/day. Net: ~$1.3k/day positive.
