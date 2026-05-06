"""Tests for HTTP caching: ETag + Cache-Control + 304 + gzip + SRI."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app

WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"
DOCS_BUILD = Path(__file__).resolve().parent.parent / "build_docs.py"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# ETag + Cache-Control
# ---------------------------------------------------------------------------
def test_summary_returns_etag_and_cache_control(client) -> None:
    r = client.get("/api/v1/summary")
    assert r.status_code == 200
    assert r.headers.get("ETag", "").startswith('W/"')
    cc = r.headers.get("Cache-Control", "")
    assert "max-age" in cc and "must-revalidate" in cc


def test_summary_returns_304_on_matching_etag(client) -> None:
    first = client.get("/api/v1/summary")
    etag = first.headers["ETag"]
    second = client.get("/api/v1/summary", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["ETag"] == etag
    # 304 must not include a body
    assert second.content == b""


def test_summary_returns_200_on_stale_etag(client) -> None:
    r = client.get("/api/v1/summary", headers={"If-None-Match": 'W/"stale-hash"'})
    assert r.status_code == 200
    assert r.headers.get("ETag")


def test_clusters_returns_etag(client) -> None:
    r = client.get("/api/v1/clusters")
    assert r.headers.get("ETag", "").startswith('W/"')


def test_etag_stable_across_requests(client) -> None:
    """Same pipeline state → same ETag."""
    e1 = client.get("/api/v1/summary").headers["ETag"]
    e2 = client.get("/api/v1/summary").headers["ETag"]
    assert e1 == e2


# ---------------------------------------------------------------------------
# gzip compression
# ---------------------------------------------------------------------------
def test_large_response_is_compressed(client) -> None:
    """The /meetings list is well over the 500-byte threshold and must compress."""
    r = client.get("/api/v1/meetings", params={"limit": 50},
                   headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    # TestClient transparently decompresses, but the header tells us the wire format
    assert r.headers.get("Content-Encoding") == "gzip"


def test_response_includes_vary_accept_encoding(client) -> None:
    """Vary header is required for any cache that handles gzip correctly."""
    r = client.get("/api/v1/meetings", params={"limit": 50},
                   headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("Vary") == "Accept-Encoding"


# ---------------------------------------------------------------------------
# Subresource Integrity (SRI) on CDN scripts
# ---------------------------------------------------------------------------
def test_dashboard_html_has_sri_on_plotly() -> None:
    html = WEB_INDEX.read_text()
    # Plotly script must be loaded with integrity + crossorigin attrs
    assert "cdn.plot.ly/plotly-2.32.0.min.js" in html, "Plotly URL must be pinned"
    plotly_block = html[html.index("plotly"):html.index("plotly") + 600]
    assert 'integrity="sha384-' in plotly_block, "Plotly must have SRI integrity"
    assert 'crossorigin="anonymous"' in plotly_block


def test_docs_template_has_sri_on_mermaid() -> None:
    """The HTML docs template embeds Mermaid via CDN — must be SRI-protected."""
    template = DOCS_BUILD.read_text()
    assert "cdn.jsdelivr.net/npm/mermaid@10.9.1" in template, \
        "Mermaid version must be pinned (not @10 floating tag)"
    # Find the mermaid script tag and verify SRI
    idx = template.index("mermaid@10.9.1")
    block = template[idx:idx + 400]
    assert 'integrity="sha384-' in block, "Mermaid must have SRI integrity"
    assert 'crossorigin="anonymous"' in block
