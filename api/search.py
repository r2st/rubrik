"""Full-text search index — local fallback + OpenSearch swap target.

Closes ADR 0008's step-6 concern (free-text dashboard search becomes
slow on Postgres at ~10M rows). This module owns three things:

  1. ``SearchIndex`` Protocol — the read interface the API talks to.
  2. ``LocalSearchIndex`` — naive in-memory string-match implementation.
     Right for development volume; the same shape as
     ``LocalDirectoryRepository``.
  3. ``OpenSearchIndex`` — production wiring (optional ``opensearch-py``
     dep). Swap by setting ``search.backend = "opensearch"`` runtime
     setting + the cluster URL.

Every query goes through the Redis-backed cache (``api/cache.py``) so
repeated queries don't re-hit the index. Cache key includes the query
string + filters + size; invalidation happens at write time when an
indexer puts a new doc (production: outbox-relayer-driven).

Trigger thresholds (ADR 0008): switch to ``OpenSearchIndex`` when
``LocalSearchIndex`` p95 query latency > 200 ms or the corpus exceeds
~10M docs (whichever comes first).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Protocol, runtime_checkable

from src.logging_config import get_logger

from . import cache as cache_mod

log = get_logger(__name__)


@dataclass(frozen=True)
class SearchHit:
    """One result from the index."""
    meeting_id: str
    title: str
    score: float
    highlights: list[str]


@runtime_checkable
class SearchIndex(Protocol):
    def query(
        self,
        text: str,
        *,
        filters: Optional[dict[str, Any]] = None,
        size: int = 20,
    ) -> list[SearchHit]: ...

    def index(self, meeting_id: str, doc: dict) -> None: ...

    def delete(self, meeting_id: str) -> None: ...


# ---------------------------------------------------------------------------
# LocalSearchIndex — dev / small-corpus implementation
# ---------------------------------------------------------------------------
class LocalSearchIndex:
    """In-memory inverted-index lite — works up to a few-hundred-thousand
    docs comfortably. Above that the SLO breach triggers a swap to
    ``OpenSearchIndex``.

    Indexing strategy: lowercase + whitespace tokenize on title + summary
    text + transcript snippet; map token → set[meeting_id]. Score by
    intersection size.

    Not a great search engine. Right for dev + the take-home volume.
    Not the production answer.
    """

    def __init__(self) -> None:
        self._docs: dict[str, dict] = {}
        self._index: dict[str, set[str]] = {}

    def index(self, meeting_id: str, doc: dict) -> None:
        self._docs[meeting_id] = doc
        text = " ".join(filter(None, (
            doc.get("title", ""),
            doc.get("summary", ""),
            doc.get("transcript_snippet", ""),
        ))).lower()
        for token in set(text.split()):
            self._index.setdefault(token, set()).add(meeting_id)

    def delete(self, meeting_id: str) -> None:
        self._docs.pop(meeting_id, None)
        for ids in self._index.values():
            ids.discard(meeting_id)

    def query(
        self,
        text: str,
        *,
        filters: Optional[dict[str, Any]] = None,
        size: int = 20,
    ) -> list[SearchHit]:
        tokens = text.lower().split()
        if not tokens:
            return []

        # Score = number of query tokens the doc contains
        candidate_scores: dict[str, int] = {}
        for tok in tokens:
            for mid in self._index.get(tok, ()):
                candidate_scores[mid] = candidate_scores.get(mid, 0) + 1

        # Apply filters (exact-match equality only — naive)
        if filters:
            filtered = {}
            for mid, score in candidate_scores.items():
                doc = self._docs.get(mid, {})
                if all(doc.get(k) == v for k, v in filters.items()):
                    filtered[mid] = score
            candidate_scores = filtered

        ranked = sorted(candidate_scores.items(), key=lambda kv: -kv[1])[:size]
        return [
            SearchHit(
                meeting_id=mid,
                title=self._docs[mid].get("title", ""),
                score=float(score),
                highlights=_highlights(self._docs[mid], tokens),
            )
            for mid, score in ranked
        ]


def _highlights(doc: dict, tokens: Iterable[str]) -> list[str]:
    """Cheap snippet extractor — first sentence containing any query token."""
    text = doc.get("summary", "") or doc.get("transcript_snippet", "")
    sentences = text.split(". ")
    out = []
    seen_tokens: set[str] = set()
    for sent in sentences:
        sent_lc = sent.lower()
        for tok in tokens:
            if tok in sent_lc and tok not in seen_tokens:
                out.append(sent.strip()[:200])
                seen_tokens.add(tok)
                break
        if len(out) >= 3:
            break
    return out


# ---------------------------------------------------------------------------
# OpenSearchIndex — production swap target
# ---------------------------------------------------------------------------
class OpenSearchIndex:
    """OpenSearch-backed index.

    Optional dep: ``opensearch-py``. The constructor raises a clear error
    if it isn't installed so deployments that don't run OpenSearch don't
    pay the import cost.

    Index name is configurable so multi-tenant deployments can keep
    per-tenant indices (or alias-route them).
    """

    def __init__(
        self,
        *,
        hosts: list[str],
        index_name: str = "transcript-intel-meetings",
        http_auth: Optional[tuple[str, str]] = None,
    ) -> None:
        try:
            from opensearchpy import OpenSearch
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "OpenSearchIndex requires the 'opensearch-py' package."
            ) from e
        self._client = OpenSearch(hosts=hosts, http_auth=http_auth, timeout=10)
        self._index_name = index_name

    def index(self, meeting_id: str, doc: dict) -> None:
        self._client.index(index=self._index_name, id=meeting_id, body=doc)

    def delete(self, meeting_id: str) -> None:
        try:
            self._client.delete(index=self._index_name, id=meeting_id)
        except Exception:  # noqa: BLE001
            log.exception("OpenSearch delete failed (id=%s)", meeting_id)

    def query(
        self,
        text: str,
        *,
        filters: Optional[dict[str, Any]] = None,
        size: int = 20,
    ) -> list[SearchHit]:
        body: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": {"multi_match": {"query": text,
                                              "fields": ["title^2", "summary",
                                                         "transcript_snippet"]}},
                }
            },
            "highlight": {"fields": {"summary": {}, "transcript_snippet": {}}},
            "size": size,
        }
        if filters:
            body["query"]["bool"]["filter"] = [
                {"term": {k: v}} for k, v in filters.items()
            ]
        resp = self._client.search(index=self._index_name, body=body)
        out: list[SearchHit] = []
        for hit in resp.get("hits", {}).get("hits", []):
            highlight_blocks = hit.get("highlight", {}) or {}
            highlights = [
                line for field_lines in highlight_blocks.values()
                for line in field_lines
            ]
            out.append(SearchHit(
                meeting_id=hit["_id"],
                title=hit.get("_source", {}).get("title", ""),
                score=float(hit.get("_score") or 0.0),
                highlights=highlights[:3],
            ))
        return out


# ---------------------------------------------------------------------------
# Cached read-through wrapper — sits in front of any SearchIndex
# ---------------------------------------------------------------------------
class CachedSearchIndex:
    """Decorator that caches query results in Redis (1-min TTL by default).

    Cache key is content-hashed over (text, filters, size). Index writes
    invalidate the entire ``search`` namespace because OpenSearch eventual
    consistency makes per-key invalidation unsound (we'd need to know
    every query a doc participated in).
    """

    NAMESPACE = "search"
    DEFAULT_TTL_SECONDS = 60

    def __init__(
        self,
        backing: SearchIndex,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.backing = backing
        self.ttl_seconds = ttl_seconds

    def index(self, meeting_id: str, doc: dict) -> None:
        self.backing.index(meeting_id, doc)
        cache_mod.cache_invalidate(self.NAMESPACE)

    def delete(self, meeting_id: str) -> None:
        self.backing.delete(meeting_id)
        cache_mod.cache_invalidate(self.NAMESPACE)

    def query(
        self,
        text: str,
        *,
        filters: Optional[dict[str, Any]] = None,
        size: int = 20,
    ) -> list[SearchHit]:
        key = cache_mod.content_hash(text, filters or {}, size)
        cached = cache_mod.cache_get(self.NAMESPACE, key)
        if cached:
            try:
                return [SearchHit(**h) for h in json.loads(cached)]
            except Exception:  # noqa: BLE001
                pass
        hits = self.backing.query(text, filters=filters, size=size)
        import contextlib
        with contextlib.suppress(Exception):
            cache_mod.cache_set(
                self.NAMESPACE, key,
                json.dumps([h.__dict__ for h in hits]),
                ttl_seconds=self.ttl_seconds,
            )
        return hits


# ---------------------------------------------------------------------------
# Default-instance helper
# ---------------------------------------------------------------------------
def default_search_index() -> SearchIndex:
    """Resolve the configured search backend (`search.backend` runtime setting).

    Resolution order:
      1. ``search.backend = "opensearch"`` + ``search.opensearch_hosts``
         → ``CachedSearchIndex(OpenSearchIndex(...))``
      2. Default → ``CachedSearchIndex(LocalSearchIndex())``
    """
    try:
        from src.runtime_settings import get_runtime
        rt = get_runtime()
        backend = str(rt.get("search.backend", "local"))
        if backend == "opensearch":
            hosts_csv = str(rt.get("search.opensearch_hosts", "")).strip()
            if hosts_csv:
                hosts = [h.strip() for h in hosts_csv.split(",") if h.strip()]
                return CachedSearchIndex(OpenSearchIndex(hosts=hosts))
            log.warning("search.backend = opensearch but no hosts configured; "
                        "falling back to local")
    except Exception:  # noqa: BLE001
        pass
    return CachedSearchIndex(LocalSearchIndex())
