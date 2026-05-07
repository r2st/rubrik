"""Background job queue scaffolding (Arq).

Heavy off-path work — ETL, batch validate, training-data prep, snapshot
rebuild — should not run on the API event loop. This module wires Arq:
the API process **enqueues**, a separate Worker Deployment **consumes**.
Worker count autoscales by Redis-queue depth via a Prometheus metric.

Usage from a route::

    from api.jobs import enqueue
    job = await enqueue("rebuild_snapshot", url="s3://bucket/snap/")
    return {"job_id": job.job_id}

Run a worker::

    arq api.jobs.WorkerSettings

Falls back to a no-op enqueue if Redis isn't configured — call sites get
a deterministic ``EnqueueResult`` so the API doesn't 500 in dev.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class EnqueueResult:
    job_id: Optional[str]
    enqueued: bool
    reason: Optional[str] = None


async def enqueue(task_name: str, **kwargs: Any) -> EnqueueResult:
    """Enqueue a job by name. No-op + warning if Redis isn't configured."""
    redis_url = _redis_url()
    if not redis_url:
        log.warning(
            "Job %r requested but no redis_url configured; running inline if "
            "possible or skipping.", task_name,
        )
        return EnqueueResult(job_id=None, enqueued=False, reason="no_redis")
    try:
        from arq import create_pool  # type: ignore[import-not-found]
        from arq.connections import RedisSettings  # type: ignore[import-not-found]
    except ImportError:
        log.warning("arq not installed; install `arq` to enable the job queue.")
        return EnqueueResult(job_id=None, enqueued=False, reason="arq_missing")
    redis = await create_pool(RedisSettings.from_dsn(redis_url))
    job = await redis.enqueue_job(task_name, **kwargs)
    job_id = getattr(job, "job_id", None) if job else None
    return EnqueueResult(job_id=job_id, enqueued=job is not None)


def _redis_url() -> Optional[str]:
    try:
        from src.settings import get_settings
        return get_settings().redis_url
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Worker tasks — each is `async def task_name(ctx, ...)`. ctx is Arq's
# per-job dict (job_id, retries, redis pool, etc.). Tasks should be
# idempotent — Arq retries on failure.
# ---------------------------------------------------------------------------
async def rebuild_snapshot(ctx: dict, url: Optional[str] = None) -> dict:
    """Rebuild + write the PipelineState snapshot.

    Equivalent to ``python -m api.snapshot_writer --url <url>``. Provided as
    a job so an admin can kick a refresh from the UI without ssh'ing into
    the cluster.
    """
    from . import snapshot
    from . import state as state_mod

    target = url
    if not target:
        try:
            from src.runtime_settings import get_runtime
            target = get_runtime().get("snapshot.url", "")
        except Exception:  # noqa: BLE001
            target = ""
    if not target:
        return {"ok": False, "reason": "no_snapshot_url"}

    fresh = await asyncio_to_thread(state_mod.reload)
    manifest = snapshot.write_snapshot(
        target, fresh, n_meetings=int(fresh.metadata.get("n_meetings", 0)),
    )
    return {"ok": True, "manifest": manifest}


async def asyncio_to_thread(fn, *args, **kwargs):
    """Local import to avoid a hard dep at module load time."""
    import asyncio
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# WorkerSettings — Arq introspects this class to wire the worker.
# ---------------------------------------------------------------------------
class WorkerSettings:
    functions = [rebuild_snapshot]
    # Conservative defaults. Override via env / Helm chart for real workloads.
    max_jobs = 4
    job_timeout = 60 * 30  # 30 minutes — pipeline rebuild can be slow
    keep_result = 60 * 60  # keep result 1h for debugging

    @staticmethod
    def get_redis_settings():  # pragma: no cover — used by Arq runtime
        from arq.connections import RedisSettings  # type: ignore[import-not-found]
        url = _redis_url()
        if not url:
            raise RuntimeError(
                "redis_url is empty — cannot start Arq worker. Set "
                "[runtime].redis_url in bootstrap.toml."
            )
        return RedisSettings.from_dsn(url)

    redis_settings = property(lambda _self: WorkerSettings.get_redis_settings())
