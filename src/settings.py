"""Bootstrap configuration loaded from `bootstrap.toml`.

Bootstrap settings are the *minimum* needed to start the service:
  - environment label, log config
  - database URL
  - admin auth bootstrap (initial password, session secret)
  - dataset path
  - observability endpoints (Sentry/OTel — these reinit on change)

Everything else (rate limits, churn-risk weights, feature flags, …) lives in
the database via `RuntimeSettings` and is managed through the admin panel.

No environment variables are read for application configuration. The only
env-equivalent override is the `BOOTSTRAP_FILE` argument to `load_bootstrap()`,
which is meant for tests / containers.
"""
from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOOTSTRAP_FILE = ROOT / "bootstrap.toml"
EXAMPLE_BOOTSTRAP_FILE = ROOT / "bootstrap.toml.example"


# ---------------------------------------------------------------------------
# Sectioned config models — one per top-level table in bootstrap.toml
# ---------------------------------------------------------------------------
class AppSection(BaseModel):
    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_format: Literal["text", "json"] = "text"


class DatabaseSection(BaseModel):
    url: str = "sqlite:///./data/admin.db"


class AdminSection(BaseModel):
    initial_password: str = "changeme-on-first-login"
    # The committed value below is a non-production placeholder. Production
    # SHOULD set ``session_secret_path`` to a file path (e.g. mounted via a
    # K8s Secret, AWS Secrets Manager, Vault) — when that path is set and
    # readable, its contents override this field. The literal in
    # bootstrap.toml is then never the production secret.
    session_secret: str = "replace-with-a-long-random-string-in-production"
    session_secret_path: str = ""

    def resolved_session_secret(self) -> str:
        """Read ``session_secret_path`` if set and readable; otherwise the
        in-line ``session_secret``. Whitespace stripped (file mounts often
        carry a trailing newline)."""
        if self.session_secret_path:
            from pathlib import Path as _P
            p = _P(self.session_secret_path)
            if p.exists():
                try:
                    return p.read_text().strip()
                except OSError:  # pragma: no cover — permission error
                    pass
        return self.session_secret


class PathsSection(BaseModel):
    dataset_path: str = ""  # empty = use repo default


class ObservabilitySection(BaseModel):
    sentry_dsn: str = ""
    otel_endpoint: str = ""
    otel_service_name: str = "transcript-intelligence"


class RuntimeSection(BaseModel):
    """Runtime-process knobs that need to be readable BEFORE the DB exists.

    These can also be promoted to runtime_settings later. For now we keep
    them in bootstrap because they affect process initialization (Redis
    URL is needed before slowapi is constructed; migrations flag is read
    by the entrypoint).
    """
    run_migrations: Literal["auto", "skip"] = "auto"
    redis_url: str = ""


class Settings(BaseModel):
    """Bootstrap settings. Loaded once at startup, immutable thereafter."""

    app: AppSection = Field(default_factory=AppSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    admin: AdminSection = Field(default_factory=AdminSection)
    paths: PathsSection = Field(default_factory=PathsSection)
    observability: ObservabilitySection = Field(default_factory=ObservabilitySection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)

    # ------------------------------------------------------------------
    # Convenience accessors (preserve existing call sites' shape)
    # ------------------------------------------------------------------
    @property
    def env(self) -> str:
        return self.app.env

    @property
    def log_level(self) -> str:
        return self.app.log_level

    @property
    def log_format(self) -> str:
        return self.app.log_format

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def database_url(self) -> str:
        return self.database.url

    @property
    def dataset_path(self) -> Optional[Path]:
        return Path(self.paths.dataset_path) if self.paths.dataset_path else None

    @property
    def sentry_dsn(self) -> Optional[str]:
        return self.observability.sentry_dsn or None

    @property
    def otel_endpoint(self) -> Optional[str]:
        return self.observability.otel_endpoint or None

    @property
    def otel_service_name(self) -> str:
        return self.observability.otel_service_name

    @property
    def redis_url(self) -> Optional[str]:
        return self.runtime.redis_url or None

    @property
    def run_migrations(self) -> str:
        return self.runtime.run_migrations


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def load_bootstrap(path: Optional[Path] = None) -> Settings:
    """Read `bootstrap.toml` (or fallback to `.example`) into a Settings object."""
    target = path or DEFAULT_BOOTSTRAP_FILE
    if not target.exists():
        target = EXAMPLE_BOOTSTRAP_FILE
    if not target.exists():
        # No file at all — return defaults (useful in tests)
        return Settings()
    with target.open("rb") as f:
        raw = tomllib.load(f)
    return Settings(**raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached bootstrap Settings instance."""
    return load_bootstrap()


# ---------------------------------------------------------------------------
# Backward compatibility — properties that USED to be on Settings directly
# now live in RuntimeSettings (the DB-backed store). The shims below are kept
# so call sites that haven't migrated yet still work, with a runtime read.
# ---------------------------------------------------------------------------
def _runtime_get(key: str, default):
    """Lazily read a runtime setting; tolerates absent DB during early bootstrap."""
    try:
        from .runtime_settings import get_runtime
        return get_runtime().get(key, default)
    except Exception:  # noqa: BLE001 — best-effort during bootstrap
        return default


# Adapter object — provides `settings.api_key`, `settings.cors_origins`, etc.
# These were previously env-driven. Now they're DB-driven via runtime_settings,
# but we expose the same attribute interface so downstream code is unchanged.
class _RuntimeBacked:
    @property
    def api_key(self) -> Optional[str]:
        return _runtime_get("auth.api_key", None) or None

    @property
    def auth_required(self) -> bool:
        return self.api_key is not None

    @property
    def cors_origins(self) -> List[str]:
        v = _runtime_get("auth.cors_origins", ["*"])
        return v if isinstance(v, list) else [v]

    @property
    def rate_limit_default(self) -> str:
        return _runtime_get("rate_limit.default", "120/minute")

    @property
    def rate_limit_strict(self) -> str:
        return _runtime_get("rate_limit.strict", "30/minute")

    @property
    def pipeline_refresh_minutes(self) -> int:
        return int(_runtime_get("pipeline.refresh_minutes", 0))

    @property
    def metrics_enabled(self) -> bool:
        return bool(_runtime_get("feature.metrics_enabled", True))

    @property
    def sentry_traces_sample_rate(self) -> float:
        return float(_runtime_get("observability.sentry_traces_sample_rate", 0.1))


_runtime_backed = _RuntimeBacked()


def get_runtime_view():
    """Return an object that proxies attribute access to RuntimeSettings.

    Call sites used to do `settings.api_key`. They can keep doing that via:
        from src.settings import get_runtime_view
        runtime = get_runtime_view()
        if runtime.auth_required: ...
    """
    return _runtime_backed
