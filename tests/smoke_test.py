"""End-to-end smoke test — hits every public + admin endpoint.

Designed to run against a *running* deployment (compose-up, K8s, prod
canary). Exits non-zero on the first non-2xx response. Output is one
line per check so it's pleasant in CI logs.

Override targets via env::

    BASE_URL=https://api.example.com \\
    ADMIN_URL=https://api.example.com \\
    ADMIN_PASSWORD=redacted \\
    python -m tests.smoke_test

The admin block is skipped silently if ``ADMIN_PASSWORD`` is not set —
that's the right default for a public-edge probe.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from typing import Optional

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_URL = os.environ.get("ADMIN_URL", "http://127.0.0.1:8001").rstrip("/")
ADMIN_PASSWORD: Optional[str] = os.environ.get("ADMIN_PASSWORD")
TIMEOUT_S = float(os.environ.get("SMOKE_TIMEOUT_S", "5.0"))

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def _get(url: str, *, headers: Optional[dict] = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""


def _post(
    url: str, *, body: bytes = b"", headers: Optional[dict] = None,
) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url, data=body, headers=headers or {}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""


_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  {PASS} {label}")
    else:
        _failures.append(label)
        suffix = f" — {detail}" if detail else ""
        print(f"  {FAIL} {label}{suffix}")


def public_smoke() -> None:
    print(f"\nPublic API · {BASE_URL}")
    for path, expected in [
        ("/api/health", 200),
        ("/api/live", 200),
        # /api/ready is allowed to be 503 if the pipeline isn't warm —
        # we just verify it's reachable and parseable.
        ("/api/ready", None),
        ("/api/v1/summary", 200),
        ("/api/v1/customers", 200),
        ("/", 200),
    ]:
        status, _ = _get(BASE_URL + path)
        if expected is None:
            check(f"GET {path} → {status}", status in (200, 503))
        else:
            check(
                f"GET {path}", status == expected,
                f"expected {expected}, got {status}",
            )


def admin_smoke() -> None:
    if not ADMIN_PASSWORD:
        print("\nAdmin API · skipped (ADMIN_PASSWORD not set)")
        return

    print(f"\nAdmin API · {ADMIN_URL}")
    import http.cookiejar
    import json

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # Login
    req = urllib.request.Request(
        ADMIN_URL + "/api/v1/admin/login",
        data=json.dumps({"password": ADMIN_PASSWORD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=TIMEOUT_S) as r:
            check("POST /admin/login", r.status == 200)
    except urllib.error.HTTPError as e:
        check("POST /admin/login", False, f"status {e.code}")
        return

    # Pull CSRF token from the cookie jar
    csrf = ""
    for c in jar:
        if c.name == "csrf_token":
            csrf = c.value
            break

    # Authed GETs
    for path in ["/api/v1/admin/me", "/api/v1/admin/settings",
                 "/api/v1/admin/audit?limit=10"]:
        try:
            with opener.open(ADMIN_URL + path, timeout=TIMEOUT_S) as r:
                check(f"GET {path}", r.status == 200)
        except urllib.error.HTTPError as e:
            check(f"GET {path}", False, f"status {e.code}")

    # Authed POST that exercises CSRF (TOTP setup is a no-op, safe to call).
    setup_req = urllib.request.Request(
        ADMIN_URL + "/api/v1/admin/totp/setup",
        data=b"",
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Sec-Fetch-Site": "same-origin",
        },
        method="POST",
    )
    try:
        with opener.open(setup_req, timeout=TIMEOUT_S) as r:
            payload = json.loads(r.read())
            check(
                "POST /admin/totp/setup (CSRF + cookie)",
                r.status == 200 and "uri" in payload,
            )
    except urllib.error.HTTPError as e:
        check("POST /admin/totp/setup (CSRF + cookie)", False, f"status {e.code}")


def main() -> int:
    print("Smoke test — Transcript Intelligence")
    public_smoke()
    admin_smoke()
    if _failures:
        print(f"\n{FAIL} {len(_failures)} failed: {_failures}")
        return 1
    print(f"\n{PASS} all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
