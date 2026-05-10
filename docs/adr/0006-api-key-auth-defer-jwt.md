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

- Single secret in the `auth.api_key` runtime setting (DB-backed via ADR 0009; rotated through `/admin`)
- `require_api_key` FastAPI dependency on every `/api/v1/*` route
- Public probes (`/api/live`, `/api/ready`, `/api/health`) **bypass auth** — load balancers and k8s probes need this
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

**Update — JWT shipped:** the JWT verifier is now shipped as `api/jwt_auth.py::require_jwt` (opt-in via the `auth.jwt_enabled` runtime setting). Supports HS256 (`auth.jwt_secret`, masked-on-read) and RS256/ES256 via JWKS endpoint (`auth.jwt_jwks_url`, cached 10 minutes). Optional `aud` / `iss` claim checks. The dependency co-exists with `require_api_key` so operators can run both during the migration window and flip routes one at a time.

**Update — admin MFA shipped:** authenticator-app TOTP via `pyotp` is now wired via `api/admin/totp.py`. Operators set up MFA through `POST /admin/totp/setup` → `/verify`; once committed, `auth.admin_totp_required = true` and `/admin/login` rejects requests that don't include a valid 6-digit code. The 401 envelope is identical for missing-code and wrong-password cases so attackers can't oracle the password. Secret is stored as the masked `secret` runtime type — never round-trips through the UI in plaintext after setup. Recovery flow: `POST /admin/totp/disable` (audited).

**Update — CSRF shipped:** `api/csrf.py::require_csrf` enforces a double-submit cookie + `Sec-Fetch-Site` check on every admin write route. Login issues a non-HttpOnly `csrf_token` cookie; the UI echoes it via `X-CSRF-Token`. Toggleable via `auth.csrf_enabled` for migration windows.

## Related

- `api/auth.py` — current implementation
- `tests/test_security.py` — covers auth disabled / enforced / health-bypass
- ADR 0005 — no database; once we add user-state, that ADR likely flips first
