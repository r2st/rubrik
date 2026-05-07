# Security Policy

## Reporting a Vulnerability

If you believe you have found a security vulnerability in this project,
please report it privately so we can fix it before public disclosure.

**Contact:** sumaninster7@gmail.com

Please include:
- A description of the issue and its potential impact.
- Steps to reproduce (proof-of-concept code, request payloads, etc.).
- Affected version / commit SHA.
- Your suggested mitigation if you have one.

We aim to:
- Acknowledge receipt within **2 business days**.
- Triage and confirm the issue within **5 business days**.
- Ship a fix or mitigation within **30 days** of confirmation, depending on
  severity.

Please do **not** open public GitHub issues, pull requests, or social-media
posts about a suspected vulnerability before it has been remediated.

## Scope

In scope:
- The FastAPI service in `api/`.
- The pipeline and data-handling code in `src/`.
- The Alembic migrations and DB schema.
- Build / deploy artifacts under `Dockerfile`, `docker-compose.yml`, `deploy/`.

Out of scope:
- Vulnerabilities in third-party dependencies (please report upstream — we
  track CVEs via `pip-audit` in CI).
- Findings that require physical access, a compromised admin account, or
  social engineering of the operator.
- Denial-of-service via volumetric traffic against an unprotected origin
  (deploy behind a CDN / WAF in production).

## Hardening Already in Place

For context, the service implements:
- PBKDF2-SHA256 password hashing for the admin account.
- HMAC-signed session cookies with `Secure` + `HttpOnly` + `SameSite=Strict`.
- Strict rate limit on `/api/v1/admin/login` and `/api/v1/admin/password`
  (5/min/IP) plus a global app-wide limiter.
- 1 MiB request body cap to prevent buffer-exhaustion DoS.
- CSP, HSTS (prod), X-Frame-Options, X-Content-Type-Options,
  Referrer-Policy, Permissions-Policy.
- Audit log for every admin mutation.

If your report touches one of these areas, please mention which control you
believe is bypassed.
