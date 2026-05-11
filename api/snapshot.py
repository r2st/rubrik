"""Shared `PipelineState` snapshot — cold-start fix.

Without this, every replica calls ``state._build()`` on first request and
rebuilds the entire pipeline from the data source. At development volume that's
seconds; at 100M records it's minutes per pod, and HPA scale-up becomes a
guaranteed latency cliff.

The fix: a singleton writer (the snapshot CLI / k8s CronJob) builds the
pipeline once per refresh interval and writes a compressed pickle to a
shared location (local fs, S3, or any filesystem ``fsspec`` understands).
Replicas read it on warm-up — typically < 5 s.

Format choice: pickle behind a manifest. Parquet would be cleaner for the
DataFrames but the ``cluster_result`` and ``incident``/``competitive`` dicts
contain non-tabular Python objects (KMeans models, nested defaultdicts),
which Parquet can't serialize without a coercion layer. Pickle is fine
because the writer and readers are the same Python version pinned in the
container; we treat the snapshot as a build artifact, not a long-lived API.

The manifest decouples staleness checks from full reads:

    snapshot_url = "/var/state/snapshot/"
        manifest.json
            { "version": "0.1.0", "built_at": "2026-05-07T08:32:11Z",
              "n_meetings": 100, "checksum": "...", "payload": "snapshot.pkl.gz" }
        snapshot.pkl.gz

Replicas poll ``manifest.json`` and reload only when the checksum changes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.logging_config import get_logger

log = get_logger(__name__)

SNAPSHOT_FORMAT_VERSION = "0.1.0"
MANIFEST_NAME = "manifest.json"
PAYLOAD_NAME = "snapshot.pkl.gz"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# URL-or-path resolution — supports local fs today; fsspec adds S3/GCS later
# ---------------------------------------------------------------------------
def _resolve(url: str) -> Any:
    """Return an object with ``read_bytes(name)`` / ``write_bytes(name, data)``
    / ``mtime(name)`` for the given URL.

    Local paths use ``Path``; ``s3://`` / ``gs://`` require ``fsspec`` to be
    installed. We import lazily so the dependency is optional.
    """
    if url.startswith(("s3://", "gs://", "az://")):
        try:
            import fsspec  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                f"Snapshot URL {url!r} needs fsspec — install s3fs/gcsfs/etc."
            ) from e
        fs, root = fsspec.core.url_to_fs(url)
        return _FsspecAdapter(fs, root)
    return _LocalPathAdapter(Path(url))


class _LocalPathAdapter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def read_bytes(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def write_bytes(self, name: str, data: bytes) -> None:
        target = self.root / name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)  # atomic on POSIX

    def exists(self, name: str) -> bool:
        return (self.root / name).exists()

    def mtime(self, name: str) -> float:
        return (self.root / name).stat().st_mtime


class _FsspecAdapter:  # pragma: no cover — exercised only when fsspec is installed
    def __init__(self, fs: Any, root: str) -> None:
        self.fs = fs
        self.root = root.rstrip("/")

    def _path(self, name: str) -> str:
        return f"{self.root}/{name}"

    def read_bytes(self, name: str) -> bytes:
        with self.fs.open(self._path(name), "rb") as f:
            return f.read()

    def write_bytes(self, name: str, data: bytes) -> None:
        with self.fs.open(self._path(name), "wb") as f:
            f.write(data)

    def exists(self, name: str) -> bool:
        return self.fs.exists(self._path(name))

    def mtime(self, name: str) -> float:
        info = self.fs.info(self._path(name))
        m = info.get("LastModified") or info.get("mtime") or info.get("modified")
        if m is None:
            return time.time()
        if hasattr(m, "timestamp"):
            return m.timestamp()
        return float(m)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _signing_secret() -> str:
    """Read the bootstrap-mounted snapshot signing key, if any.

    Lazy import so ``api.snapshot`` stays importable in standalone tools
    that don't load the full settings stack.
    """
    try:
        from src.settings import get_settings
        return get_settings().resolved_snapshot_signing_secret()
    except Exception:  # noqa: BLE001
        return ""


def _sign_manifest(manifest: dict, secret: str) -> str:
    """HMAC-SHA256 over the canonical-JSON manifest. Stable encoding so
    operators can verify out-of-band with `openssl dgst -hmac`.
    """
    import hmac as _hmac
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def write_snapshot(url: str, payload: Any, *, n_meetings: int) -> dict:
    """Serialize + write to ``url``. Returns the manifest dict.

    Atomic at the filesystem level (write-tmp-then-rename).

    If the admin section's ``snapshot_signing_secret`` (or its
    file-mounted variant) is set, an HMAC-SHA256 signature is added to
    the manifest. ``read_snapshot`` then refuses to load any payload
    whose signature doesn't verify — closing the pickle-RCE primitive
    that an unsigned snapshot otherwise creates if the storage backend
    is ever compromised.
    """
    storage = _resolve(url)
    raw = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    blob = gzip.compress(raw, compresslevel=6)
    checksum = hashlib.sha256(blob).hexdigest()

    storage.write_bytes(PAYLOAD_NAME, blob)
    manifest = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "built_at": _now_iso(),
        "n_meetings": n_meetings,
        "checksum": checksum,
        "payload": PAYLOAD_NAME,
        "size_bytes": len(blob),
    }
    secret = _signing_secret()
    if secret:
        # Sign the manifest's content fields (NOT the signature itself).
        manifest["signature"] = _sign_manifest(manifest, secret)
        manifest["signature_alg"] = "HMAC-SHA256"
    storage.write_bytes(MANIFEST_NAME, json.dumps(manifest, indent=2).encode("utf-8"))
    log.info(
        "Snapshot written: url=%s n_meetings=%d size=%d bytes checksum=%s… signed=%s",
        url, n_meetings, len(blob), checksum[:12], bool(secret),
    )
    return manifest


def read_manifest(url: str) -> Optional[dict]:
    """Read the manifest, or None if no snapshot exists yet."""
    try:
        storage = _resolve(url)
        if not storage.exists(MANIFEST_NAME):
            return None
        return json.loads(storage.read_bytes(MANIFEST_NAME).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Snapshot manifest read failed (%s): %s", url, e)
        return None


def read_snapshot(url: str) -> Optional[Any]:  # noqa: PLR0911
    """Read + verify the snapshot payload. Returns the unpickled object or None.

    Returns None on any error (missing file, checksum mismatch, version skew) —
    the caller is expected to fall back to a fresh build. Failing-loud here
    would defeat the cold-start fix.
    """
    manifest = read_manifest(url)
    if manifest is None:
        return None
    if manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        log.warning(
            "Snapshot format version mismatch (got %s, want %s) — falling back to build",
            manifest.get("format_version"), SNAPSHOT_FORMAT_VERSION,
        )
        return None

    # Signature gate — runs BEFORE we touch the payload. An unsigned
    # snapshot is silently accepted only when no signing secret is
    # configured (legacy / dev). Once the operator mounts a key, every
    # snapshot must verify against it: a missing or bad signature is a
    # hard "refuse to load + log loudly" outcome, NOT a fall-through.
    secret = _signing_secret()
    if secret:
        sig = manifest.get("signature")
        if not sig:
            log.warning(
                "Snapshot is unsigned but a signing key is configured — "
                "refusing to load (possible storage compromise)",
            )
            return None
        unsigned = {k: v for k, v in manifest.items()
                    if k not in ("signature", "signature_alg")}
        expected = _sign_manifest(unsigned, secret)
        import hmac as _hmac
        if not _hmac.compare_digest(sig, expected):
            log.error(
                "Snapshot signature mismatch — refusing to load. "
                "Either the signing key rotated or the snapshot was tampered.",
            )
            return None

    try:
        storage = _resolve(url)
        blob = storage.read_bytes(manifest["payload"])
    except Exception as e:  # noqa: BLE001
        log.warning("Snapshot payload read failed (%s): %s", url, e)
        return None

    actual = hashlib.sha256(blob).hexdigest()
    if actual != manifest["checksum"]:
        log.warning("Snapshot checksum mismatch — falling back to build")
        return None

    try:
        return pickle.loads(gzip.decompress(blob))
    except Exception as e:  # noqa: BLE001
        log.warning("Snapshot deserialize failed: %s", e)
        return None
