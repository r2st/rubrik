"""Snapshot writer — runs the pipeline once and writes the result for replicas.

Invoked as the singleton refresh job (k8s CronJob in production, ``make
snapshot`` in dev). Pure CLI — no FastAPI app, no event loop.

Replicas detect a fresh snapshot via the manifest's checksum and reload
without restarting.

Usage::

    python -m api.snapshot_writer --url /var/state/snapshot/
    python -m api.snapshot_writer --url s3://my-bucket/snapshots/prod/

If ``--url`` is omitted, falls back to the ``snapshot.url`` runtime setting.
"""
from __future__ import annotations

import argparse
import sys

from src.logging_config import configure_logging, get_logger

from . import snapshot, state

log = get_logger(__name__)


def _resolve_url(arg_url: str | None) -> str:
    if arg_url:
        return arg_url
    try:
        from src.runtime_settings import get_runtime
        url = get_runtime().get("snapshot.url", "")
    except Exception:  # noqa: BLE001
        url = ""
    if not url:
        log.error(
            "No snapshot URL provided. Pass --url or set the snapshot.url "
            "runtime setting in the admin panel."
        )
        sys.exit(2)
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build + write a PipelineState snapshot.")
    parser.add_argument(
        "--url",
        help="Snapshot destination (path or s3://...). Falls back to runtime setting "
             "snapshot.url if omitted.",
    )
    parser.add_argument(
        "--log-format", choices=["text", "json"], default="text",
        help="Log output format.",
    )
    args = parser.parse_args(argv)
    configure_logging(level="INFO", fmt=args.log_format)

    url = _resolve_url(args.url)
    log.info("Building pipeline snapshot for url=%s", url)

    # Force a fresh build, ignoring any in-process cache.
    fresh = state.reload()
    manifest = snapshot.write_snapshot(
        url, fresh, n_meetings=int(fresh.metadata.get("n_meetings", 0)),
    )
    log.info("Snapshot complete: %s", manifest)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
