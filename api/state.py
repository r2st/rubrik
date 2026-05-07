"""Pipeline state — runs the analysis once at startup, caches the result.

The dataset is largely static, so we trade memory for response latency. For
multi-instance deployment, swap this for a shared cache (Redis) or run the
pipeline as a batch job and serve from a persisted store.

Optional periodic refresh: if `settings.pipeline_refresh_minutes > 0`, an
asyncio task rebuilds the state on that cadence so the API picks up new
meeting JSON files without a process restart.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src import categorizer, clustering, data_loader, insights, sentiment
from src.logging_config import get_logger
from src.repository import TranscriptRepository, default_repository

log = get_logger(__name__)


@dataclass
class PipelineState:
    df: pd.DataFrame
    sentences_df: pd.DataFrame
    speakers_df: pd.DataFrame
    cluster_result: Any
    health: pd.DataFrame
    incident: dict[str, Any]
    ai_load: pd.DataFrame
    competitive: dict[str, Any]
    dominance: pd.DataFrame
    pivots: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)
    # When this build completed (monotonic seconds since process start).
    # Used to compute the X-State-Age-Seconds response header.
    built_at_monotonic: float = 0.0


_state: PipelineState | None = None
_lock = threading.Lock()
_refresh_task: asyncio.Task | None = None
_snapshot_poll_task: asyncio.Task | None = None
_consecutive_refresh_failures: int = 0
_refresh_interval_minutes: int = 0
# Active repository — resolved lazily so tests can override before first use.
_repository: TranscriptRepository | None = None
# Last manifest checksum we loaded — drives snapshot-poll reload decisions.
_loaded_snapshot_checksum: str | None = None


def set_repository(repo: TranscriptRepository | None) -> None:
    """Override the data source. Tests use this to inject fixtures; production
    code leaves it `None` so `_build()` falls back to `default_repository()`."""
    global _repository, _state
    _repository = repo
    _state = None  # force rebuild on next get_state()


def get_state() -> PipelineState:
    """Return the cached pipeline state, building it on first call."""
    global _state
    if _state is None:
        with _lock:
            if _state is None:
                _state = _build()
    return _state


def reload() -> PipelineState:
    """Force a synchronous rebuild — useful for tests or manual refresh."""
    global _state
    with _lock:
        _state = _build()
    return _state


def is_warm() -> bool:
    """True iff a usable PipelineState is loaded (for the readiness probe)."""
    return _state is not None


def _try_load_snapshot() -> PipelineState | None:
    """Try to satisfy the build from a shared snapshot — cold-start fix.

    Returns None if no snapshot is configured, the manifest is missing, or
    the payload fails verification. Caller falls back to building from the
    repository.
    """
    global _loaded_snapshot_checksum
    try:
        from src.runtime_settings import get_runtime
        url = get_runtime().get("snapshot.url", "")
    except Exception:  # noqa: BLE001
        url = ""
    if not url:
        return None
    from . import snapshot as snap
    manifest = snap.read_manifest(url)
    if manifest is None:
        return None
    payload = snap.read_snapshot(url)
    if payload is None:
        return None
    if not isinstance(payload, PipelineState):
        log.warning("Snapshot is not a PipelineState — falling back to build")
        return None
    payload.built_at_monotonic = time.monotonic()
    _loaded_snapshot_checksum = manifest.get("checksum")
    log.info("Loaded pipeline state from snapshot (checksum=%s…, n_meetings=%d)",
             (manifest.get("checksum") or "")[:12], manifest.get("n_meetings", 0))
    return payload


async def start_snapshot_poll(interval_seconds: int = 30) -> None:
    """Watch the snapshot manifest for checksum changes; reload when it bumps.

    No-op if ``snapshot.url`` is unset or the task is already running. The
    poll cadence defaults to 30s — short enough that operators see refreshes
    promptly, long enough to keep S3 GET cost negligible.
    """
    global _snapshot_poll_task
    if _snapshot_poll_task is not None:
        return
    try:
        from src.runtime_settings import get_runtime
        url = get_runtime().get("snapshot.url", "")
    except Exception:  # noqa: BLE001
        url = ""
    if not url:
        return

    async def _loop() -> None:
        global _state, _loaded_snapshot_checksum
        from . import snapshot as snap
        log.info("Snapshot poll starting (url=%s, every %ds)", url, interval_seconds)
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    manifest = await asyncio.to_thread(snap.read_manifest, url)
                    if manifest is None:
                        continue
                    if manifest.get("checksum") == _loaded_snapshot_checksum:
                        continue
                    payload = await asyncio.to_thread(snap.read_snapshot, url)
                    if isinstance(payload, PipelineState):
                        payload.built_at_monotonic = time.monotonic()
                        with _lock:
                            _state = payload
                        _loaded_snapshot_checksum = manifest.get("checksum")
                        log.info("Snapshot reloaded (checksum=%s…)",
                                 (_loaded_snapshot_checksum or "")[:12])
                except Exception:  # noqa: BLE001
                    log.exception("Snapshot poll iteration failed; will retry")
        except asyncio.CancelledError:
            log.info("Snapshot poll cancelled")
            raise

    _snapshot_poll_task = asyncio.create_task(_loop(), name="snapshot-poll")


async def stop_snapshot_poll() -> None:
    global _snapshot_poll_task
    if _snapshot_poll_task is None:
        return
    _snapshot_poll_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _snapshot_poll_task
    _snapshot_poll_task = None


async def start_refresh_task(interval_minutes: int) -> None:
    """Start the background refresh loop. No-op if interval <= 0 or already running."""
    global _refresh_task, _refresh_interval_minutes
    if interval_minutes <= 0 or _refresh_task is not None:
        return
    _refresh_interval_minutes = interval_minutes

    async def _loop() -> None:
        global _consecutive_refresh_failures
        log.info("Pipeline refresh loop starting (every %d min)", interval_minutes)
        try:
            while True:
                await asyncio.sleep(interval_minutes * 60)
                try:
                    await asyncio.to_thread(reload)
                    _consecutive_refresh_failures = 0
                    log.info("Pipeline state refreshed")
                except Exception:  # noqa: BLE001
                    _consecutive_refresh_failures += 1
                    log.exception(
                        "Pipeline refresh failed (consecutive=%d); "
                        "will retry next cycle. Serving last-good state.",
                        _consecutive_refresh_failures,
                    )
        except asyncio.CancelledError:
            log.info("Pipeline refresh loop cancelled")
            raise

    _refresh_task = asyncio.create_task(_loop(), name="pipeline-refresh")


async def stop_refresh_task() -> None:
    """Cancel the refresh task on shutdown."""
    global _refresh_task
    if _refresh_task is None:
        return
    _refresh_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _refresh_task
    _refresh_task = None


def _build() -> PipelineState:
    # Cold-start fix: try the shared snapshot first. Replicas read instead of
    # rebuilding; a singleton CronJob writes (see api/snapshot_writer.py).
    snap_state = _try_load_snapshot()
    if snap_state is not None:
        return snap_state

    log.info("Building pipeline state…")
    repo = _repository if _repository is not None else default_repository()
    raw = repo.all()
    df = data_loader.meetings_to_dataframe(raw)
    sentences_df = data_loader.sentences_dataframe(raw)
    speakers_df = data_loader.speakers_dataframe(raw)

    df = categorizer.annotate(df)
    df["num_action_items"] = df["action_items"].apply(len)
    df = sentiment.add_trajectories(df, sentences_df)

    cluster_result = clustering.cluster_transcripts(df["full_transcript"])
    df["content_cluster"] = cluster_result.labels

    health = insights.customer_health(df)
    incident = insights.incident_impact(df)
    ai_load = insights.action_item_load(df, top_n=15)
    competitive = insights.competitive_signals(df)
    dominance = insights.speaker_dominance(speakers_df, df)
    pivots = insights.negative_pivots(df)

    log.info(
        "Pipeline ready: %d meetings, k=%d (silhouette=%.3f), %d at-risk customers",
        len(df), cluster_result.n_clusters, cluster_result.silhouette,
        len(health[health["risk_tier"] == "🔴 high"]) if len(health) else 0,
    )

    return PipelineState(
        df=df,
        sentences_df=sentences_df,
        speakers_df=speakers_df,
        cluster_result=cluster_result,
        health=health,
        incident=incident,
        ai_load=ai_load,
        competitive=competitive,
        dominance=dominance,
        pivots=pivots,
        metadata={
            "n_meetings": len(df),
            "date_range": [str(df["start_time"].min().date()),
                           str(df["start_time"].max().date())],
            "n_clusters": cluster_result.n_clusters,
            "silhouette": round(cluster_result.silhouette, 3),
        },
        built_at_monotonic=time.monotonic(),
    )


def state_age_seconds() -> int:
    """Seconds since the current state was built. 0 if no state yet."""
    if _state is None or _state.built_at_monotonic == 0:
        return 0
    return int(time.monotonic() - _state.built_at_monotonic)


def is_stale() -> bool:
    """True if pipeline refresh has been failing for >2× the refresh interval.

    No-op (always False) if the refresh loop isn't enabled.
    """
    if _refresh_interval_minutes <= 0:
        return False
    threshold_s = _refresh_interval_minutes * 60 * 2
    return state_age_seconds() > threshold_s and _consecutive_refresh_failures > 0
