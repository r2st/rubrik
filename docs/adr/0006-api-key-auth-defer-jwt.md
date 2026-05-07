# ADR 0006: API Key Auth; Defer JWT/OAuth

- **Status:** Accepted
- **Date:** 2026-05-06

## Context

Every `/api/v1/*` endpoint exposes meeting data, customer health scores, and incident analyses — all of which are sensitive. The service was previously *open*: anyone with the URL could query everything. That's not viable for production.

Three auth models on the table:
1. **Static shared API key** (`X-API-Key` header)
2. **JWT** with claims (sub, scopes, exp) — stateless, well-supported
3. **OAuth2 / OIDC** with an identity provider (Auth0, Okta, Cognito)

The cost ladder:
- API key: ~10 lines of code, no infra
- JWT: ~50 lines, secret management, signing keys, refresh strategy
- OAuth2: identity provider, callback flow, session store, JWKS rotation

The current product state:
- Single tenant. There's one consumer of the API: the dashboard
- No per-user state (no annotations, no preferences, no per-user data isolation)
- No public-facing self-service signup

JWT and OAuth2 buy us **multi-tenancy and per-user attribution** — neither of which we need yet.

## Decision

We **ship a static shared API key**, with the option to disable auth entirely in dev (no `API_KEY` env → open access).

- Single secret in `Settings.api_key` (env var, never committed)
- `require_api_key` FastAPI dependency on every `/api/v1/*` route
- Health probe (`/api/health`) **bypasses auth** — load balancers and k8s probes need this
- Failed auth returns the standardized error envelope with `code=unauthorized`

The implementation is in `api/auth.py`. Total surface: ~30 lines.

### Admin credential endpoints

`POST /api/v1/admin/login` and `POST /api/v1/admin/password` are protected by a stricter, dedicated rate limit (5 requests/minute/IP) layered on top of the global slowapi middleware. It's implemented as a small in-memory sliding-window FastAPI dependency (`strict_rate_limit` in `api/admin/routes.py`) — slowapi's decorator approach interfered with FastAPI body-model introspection on newer pydantic, so we keep the route signatures clean and cap requests via `Depends`. Per-process bound is fine for our replica count; promoting the counter to Redis is a one-function swap when we need cluster-wide enforcement.

## Consequences

**Positive**
- Production deployment now gates access behind a secret — meaningful security improvement
- Dev workflow unchanged (no key set → open access)
- Zero infrastructure required (no IDP, no token issuer)
- Future-proof: swapping `require_api_key` for a JWT verifier is a one-line dependency change in the router

**Negative**
- **No per-user attribution.** Audit logs can correlate a request ID with an IP, but not with a user
- **No fine-grained scopes.** Anyone with the key sees everything — appropriate for a single-tenant service-to-service call, not for multi-user
- **Manual key rotation.** No expiry, no automated rotation; needs operational discipline

## When to revisit

- We add **multi-tenancy** — per-tenant data isolation requires identity, not just a shared secret
- We add **multiple consumer roles** (admin, viewer, integrator) — needs scopes
- A customer demands **per-user audit logs** for compliance (SOC 2, HIPAA, etc.)
- We expose the API to **untrusted clients** (mobile apps, third-party developers) — token revocation matters

The migration path is clear: replace `require_api_key` with a JWT verifier, add an issuance endpoint or use an external IDP. Routes stay unchanged; only the dependency is swapped.

## Related

- `api/auth.py` — current implementation
- `tests/test_security.py` — covers auth disabled / enforced / health-bypass
- ADR 0005 — no database; once we add user-state, that ADR likely flips first
