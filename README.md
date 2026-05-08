# Transcript Intelligence

> An **auto-scalable system** that processes B2B meeting transcripts and surfaces topic categorization, sentiment trends, and strategic insights — exposed as a REST API with a lightweight web dashboard. **Target scale: millions to 100M+ records.**

[![CI](https://img.shields.io/badge/CI-GitHub%20Actions-blue)](.github/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-222%20passing-brightgreen)](tests/)
[![Coverage](https://img.shields.io/badge/coverage-94%25-brightgreen)](pyproject.toml)
[![Validation](https://img.shields.io/badge/validation-9%2F10%20pass-brightgreen)](validate.py)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

---

## Contents

- [What this does](#what-this-does)
- [Illustrative findings](#illustrative-findings)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Testing & validation](#testing--validation)
- [API & web dashboard](#api--web-dashboard)
- [Run all services](#run-all-services)
- [Production readiness](#production-readiness)
- [Documentation](#documentation)

---

## Scope: an auto-scalable system

**The target is an auto-scalable system that handles millions to 100M+ meeting records.** Every component states its scale envelope and the substrate it switches to past that envelope.

The codebase reflects that:

| Layer | Designed for | Path to scale |
|---|---|---|
| Pipeline correctness | Any volume | Component-by-component scaling envelopes below |
| In-memory pandas analysis | ≤ ~100k records | Switch to streaming + repository pattern → ADR 0008 |
| FastAPI service | Stateless, horizontal scale | Replicate behind LB; cache via Redis (ADR 0014) |
| **LLM tier 1 — self-hosted Gemma 4** (QLoRA fine-tune) | Bulk summarization + action-item extraction; ~150 ms per call | Multi-node Ray Train + autoscaled vLLM with multi-tenant LoRA hot-swap → ADR 0010 |
| **LLM tier 2 — frontier API** (Claude / GPT-4 / Gemini) | ~5% of traffic: out-of-distribution, long-context, world-knowledge, high-stakes | Per-tenant cap + budget gateway; admin-tunable via the `llm.tier2_*` runtime settings → ADR 0012 |
| Admin panel + runtime config | Same operational surface at any scale | Scales as the API scales; runs as a separate process on its own port (ADR 0014) |
| Event backbone + outbox | Decouples writes from fan-out; replayable | Kafka via Strimzi (`deploy/k8s/kafka.yaml`); outbox model → relayer → topic (ADR 0014) |

Wherever a design decision depends on scale, the doc states the **scale envelope** explicitly (e.g., "TF-IDF + KMeans is sound up to ~1M docs in-memory; switch to streaming/minibatch above that").

### LLM choice: why Gemma 4 over the open-model alternatives

| Family | Sizes | License | Why we didn't pick it (vs Gemma 4) |
|---|---|---|---|
| **Gemma 4** (chosen) | E2B / E4B (effective compute) | Gemma Terms (commercial use OK; some restrictions) | — |
| Llama 3.2 | 1B / 3B / 8B / 70B / 405B | Llama Community License (free up to 700 M MAU) | At our task profile (summarization + bulleted extraction) the small variants don't hit Gemma 4 E4B's quality on a single-H100 LoRA run; the larger variants don't fit our latency budget on a single L4 inference pod. |
| Qwen 2.5 | 0.5B – 72B | Apache 2.0 (most sizes) | Strong, Apache-licensed, good ecosystem. Lost on **multimodal-ready** for our roadmap (video / screen-share processing). Comparable quality at 7B; we'd happily switch if Gemma's license becomes a blocker. |
| Mistral 7B / Mixtral | 7B / 8×7B / 8×22B | Apache 2.0 | Mixtral's MoE is overkill for summarization; Mistral 7B is dense at the size where Gemma's E-series sparse-effective design wins on inference cost. |
| Phi-3 (Microsoft) | 3.8B / 14B / mini-128k | MIT | Strong on reasoning benchmarks; weaker on long-form English summarization style match in our held-out eval. |
| DeepSeek V2 / V3 | 16B-effective / 671B | DeepSeek License | Excellent reasoning; total VRAM needs (even with MoE) overshoot the single-H100 fine-tune budget, and the operator footprint is heavier than we want for the bulk path. |

**Why Gemma 4 specifically:**
1. **Sparse-effective compute (E-series).** E2B and E4B run at compute parity with their effective size while quality tracks the much larger dense models — right point on the cost-vs-quality curve for self-hosting.
2. **Native multimodal.** E4B accepts image input. We don't use it today, but screen-share + slide-deck context is a likely roadmap item; switching families later costs more than buying the optionality up front.
3. **Tooling maturity.** First-class support in `unsloth` (the QLoRA pipeline we use), `vLLM` (the serving stack ADR 0010 commits to), Hugging Face's `transformers`, and PEFT. Not all alternatives ship that combination cleanly today.
4. **Memory profile.** QLoRA on E4B fits comfortably on a single H100 (we measured: ~28 min, ~$1.40 wall-clock for the recipe-validation run). Llama 70B doesn't; Qwen 32B is borderline.
5. **Style transfer worked.** v3 hit ROUGE-L 0.394 (+38% over the untuned E4B baseline of 0.286) and held-out outputs visibly matched reference voice. The recipe transfers.

**Honest tradeoffs:**
- Gemma's license is **more restrictive than Apache 2.0** (e.g., Qwen, Mistral). Tolerable for our use; documented as a "When to revisit" trigger in ADR 0003.
- Smaller community than Llama → fewer pre-trained domain adapters to start from.
- Newer family → less battle-tested in production at our intended scale.

If any of those tradeoffs flip, the swap is a one-file change in `gemma-finetune/code/finetune_v3.py` (`base_model` constant) and a re-run of the same training recipe — the rest of the pipeline is model-agnostic. Full comparison + four-iteration training results: [`docs/adr/0003-self-host-summarization-with-gemma-4.md`](docs/adr/0003-self-host-summarization-with-gemma-4.md).

## What this does

For a stream of B2B meeting transcripts (support cases, customer-facing calls, internal meetings) — designed to scale to millions / 100M+ — this system:

1. **Categorizes** every meeting along three dimensions — call type, purpose, product area — using regex rules + TF-IDF clustering
2. **Analyzes sentiment** at meeting *and* sentence granularity, surfacing within-call friction moments invisible to summary-level scores
3. **Generates six strategic insights** — customer churn risk, incident blast radius, action item bottlenecks, competitive language, speaker dominance, within-meeting negative pivots

Five surfaces over the same `src/` analysis core:

| Surface | When to use |
|---|---|
| `transcript_intelligence.ipynb` | Reviewable narrative — the deliverable |
| `api/` (FastAPI + Plotly.js dashboard at `/`) | Live demo, drill-downs, production-grade |
| `run_analysis.py` | Batch / CI / scheduled refresh |
| `validate.py` | Semantic audits against the dataset |
| `docs/html/` | Standalone HTML docs (no server needed) |

Plus a separate experiment in [`gemma-finetune/`](gemma-finetune/README.md): fine-tunes **Gemma 4 (E4B)** to demonstrate a self-hosted alternative to vendor LLM APIs (ROUGE-L 0.39 vs 0.29 baseline). See [APPROACH §Summarization](docs/APPROACH.md#2-summarization--action-items) for the verdict, and [`gemma-finetune/scaling/`](gemma-finetune/scaling/README.md) + [ADR 0010](docs/adr/0010-auto-scaling-ml-pipeline.md) for the production auto-scaling architecture (Ray Train + FSDP for training, vLLM + HPA for serving, active learning for continuous improvement).

## Illustrative findings

These illustrate **the kind of insight the pipeline produces** — at production volume the same layers surface analogous patterns at much larger scale.

| Area | Headline |
|---|---|
| Categorization | 3 call types · 11 purposes · 4 product areas. **k=7** content clusters (silhouette-selected). |
| Sentiment | Support 2.94 < internal 3.42 < external 3.71. Detect 3.20 — outage drag. |
| Outage impact | A single incident dragged sentiment by **0.77 points** across affected meetings. |
| Top at-risk customers | Northstar Pharma · Cobalt Software · Summit Trust |
| Execution bottleneck | One owner accumulated 31 action items — far more than any peer |
| Conversation health | Support calls have **51% single-speaker dominance** — agents may be over-talking |
| Friction moments | Sentence-level analysis surfaces sharp within-call sentiment drops invisible to summary scores |

## Quick start

**Run everything with one command:**

```bash
make install-dev   # install + dev tools + pre-commit hooks
make start-all     # ./bin/start-all.sh — see "Run all services" below
```

**Or run pieces individually:**

```bash
make test          # 71 tests across rules, sentiment, clusters, insights, API
make validate      # 10 semantic audits against the dataset
make dev           # FastAPI server with hot reload → http://127.0.0.1:8000
make docker-build  # containerized
make docs          # static HTML site at docs/html/
```

**Without Make:**

```bash
pip install -e ".[dev]"
pytest && python validate.py && python run_analysis.py
uvicorn api.main:app --reload
```

### Production-volume mode

The default `run_analysis.py` loads the entire dataset into pandas — fine for development, fails at production volume. For 1M+ records use the streaming pipeline:

```bash
# Streaming — memory is O(batch_size), regardless of total dataset size
python run_analysis.py --streaming --batch-size 1000

# Equivalent results to the in-memory pipeline (verified by tests/test_streaming.py)
# Skips clustering + visualizations (computed in the columnar warehouse at scale)
```

Backed by `src/repository.py` (Protocol + `LocalDirectoryRepository` today; `DatabaseRepository` slot for ADR 0008's Postgres + Iceberg backend) and `src/streaming.py` (mergeable fold — trivially parallelizes across Ray Data workers).

For schema migrations (`bootstrap.toml` Postgres URL):

```bash
alembic upgrade head           # apply
alembic revision --autogenerate -m "add foo column"
alembic downgrade -1           # roll back one
```

The Docker image runs `alembic upgrade head` on container start (entrypoint), so production-equivalent boots always apply pending migrations before `uvicorn` starts. Migrations are idempotent; Alembic's lock makes it safe under multi-replica boots.

## Architecture

```mermaid
flowchart LR
    Data[("Filesystem (JSON)<br/>scales out via ADR 0008")] --> Loader["data_loader<br/>typed DataFrames"]

    Loader --> Categorizer["categorizer<br/>regex rules"]
    Loader --> Sentiment["sentiment<br/>per-sentence trajectories"]
    Loader --> Clustering["clustering<br/>TF-IDF + KMeans"]

    Categorizer --> Insights["insights<br/>6 modules"]
    Sentiment --> Insights
    Clustering --> Insights

    Config[("config<br/>keywords, thresholds")] -.-> Categorizer
    Config -.-> Clustering
    Config -.-> Insights

    Insights --> API["🚀 FastAPI<br/>REST API + dashboard"]
    Insights --> Notebook["📓 Notebook<br/>narrative"]
    Insights --> CLI["⚙️ run_analysis.py<br/>batch"]
    Insights --> Validator["🔍 validate.py<br/>audits"]

    API --> Web[("Web UI<br/>Plotly.js · vanilla JS")]
    CLI --> Out[("output/<br/>CSV · JSON · PNG")]
```

The four interfaces all import the same `src/` modules — single source of truth, no duplicated logic.

→ See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the module dependency, data model, and pipeline stage diagrams.
→ See [`docs/APPROACH.md`](docs/APPROACH.md) for the methodology decisions and verdicts.

## Project layout

```
transcript-intelligence/
├── pyproject.toml                # PEP 621 packaging + ruff + mypy + pytest config
├── requirements.txt              # runtime deps (also installable via pyproject)
├── Makefile                      # common commands
├── Dockerfile                    # multi-stage, non-root, JSON logs, healthcheck
├── docker-compose.yml            # API + optional Caddy reverse proxy
├── .pre-commit-config.yaml
├── .github/workflows/ci.yml      # lint · type-check · test · Docker build
├── bin/
│   ├── start-all.sh              # one-command launcher (API + Jupyter + docs)
│   └── stop-all.sh               # graceful teardown
├── deploy/Caddyfile              # reverse-proxy config for compose
├── run_analysis.py               # batch pipeline
├── validate.py                   # semantic audits
├── build_docs.py                 # MD → HTML
├── transcript_intelligence.ipynb # narrative notebook
├── src/                          # analysis core (importable package)
│   ├── config.py                 # keyword maps, thresholds (single source of truth)
│   ├── data_loader.py            # raw JSON → typed DataFrames + dataclass
│   ├── categorizer.py            # call type / purpose / product / customer
│   ├── sentiment.py              # meeting + sentence-level trajectories
│   ├── clustering.py             # TF-IDF + KMeans, k via silhouette
│   ├── insights.py               # 6 strategic insights
│   ├── visualizations.py         # matplotlib (notebook + CLI)
│   └── logging_config.py         # structured logging (text or JSON)
├── api/                          # FastAPI service
│   ├── main.py                   # app, lifespan, static mount
│   ├── routes.py                 # /api/* endpoints with OpenAPI auto-docs
│   ├── models.py                 # Pydantic response schemas
│   └── state.py                  # cached pipeline (thread-safe singleton)
├── web/                          # static frontend (no build step)
│   ├── index.html
│   └── static/{app.js, style.css, favicon.svg}
├── tests/                        # 71 tests · 94% coverage
├── docs/
│   ├── ARCHITECTURE.md           # system design with Mermaid diagrams
│   ├── APPROACH.md               # methodology decisions + verdicts
│   └── html/                     # built static site (make docs)
├── gemma-finetune/               # Gemma 4 fine-tuning experiment (separate)
│   ├── README.md                 # methodology + 4 training iterations
│   ├── code/                     # finetune_v3.py, finetune_v4.py, judge.py …
│   ├── data/                     # 380 train rows + 5 held-out eval prompts
│   ├── adapters/                 # LoRA adapters (weights gitignored, 477 MB)
│   └── results/                  # train logs + per-meeting metric JSONs
└── output/                       # generated artifacts (gitignored)
```

## Testing & validation

Three complementary layers:

| Layer | Command | What it checks |
|---|---|---|
| **Unit + integration tests** | `make test` | 71 tests, 94% coverage. Categorizer, sentiment math, clustering, insights, end-to-end API |
| **Semantic validation** | `make validate` | 10 audits against the *actual data* — rule coverage, cross-references, distribution checks |
| **Lint + type-check** | `make lint && make type-check` | ruff (style + bugbear + simplify) + mypy |

```bash
$ make test
71 passed in 2.69s · coverage: 94%

$ make validate
9 pass · 1 warn · 0 fail (10 checks)
```

The remaining warning (cluster homogeneity) is a real finding — two clusters re-discover rule categories — not a defect.

## API & web dashboard

```bash
make dev   # http://127.0.0.1:8000
```

The web app at `/` consumes the same JSON endpoints any external client would. OpenAPI docs at `/docs`.

| Endpoint group | Examples |
|---|---|
| **Probes** | `GET /api/live` (process up) · `GET /api/ready` (warm + DB + not draining) · `GET /api/health` (combined, legacy) |
| **Meta** | `GET /api/summary` · `GET /metrics` (Prometheus, opt-in) |
| **Admin** | `POST /api/v1/admin/login` · `POST /api/v1/admin/snapshot/rebuild` · settings CRUD · audit log |
| **Meetings** | `GET /api/meetings?call_type=&product=&date_from=…` · `GET /api/meetings/{id}` |
| **Sentiment** | `GET /api/sentiment/{by-call-type, by-purpose, weekly, scores}` |
| **Clusters** | `GET /api/clusters` |
| **Insights** | `GET /api/insights/{customer-health, customer/{name}, incident-impact, action-items, competitive, speaker-dominance, negative-pivots}` |

### Why FastAPI instead of Streamlit

| Concern | Streamlit | FastAPI + static frontend |
|---|---|---|
| Multi-user / scale-out | Single session per process | Stateless, scales horizontally |
| API contract | None — UI-only | OpenAPI schema, versioned models |
| Testability | Hard to test the UI logic | `TestClient` covers every endpoint |
| Deployment | Streamlit-specific runtime | Standard ASGI / Docker / Kubernetes |
| Frontend flexibility | Streamlit components only | Any client (web, mobile, BI tool) |

## Run all services

A single command brings up the whole dev environment:

```bash
./bin/start-all.sh   # pre-flight + start everything
./bin/stop-all.sh    # kill anything left running
```

What it does (in order):
1. **Refreshes** `output/` (batch pipeline) and `docs/html/` (HTML docs) in parallel
2. **Starts** four services in the background, waits for each to be ready, prints the URLs:

By default, **tests and validation are NOT run** — that's a slow opt-in. Use the dedicated targets when you want them: `make test`, `make validate`. Or pass them as flags to the launcher:

```bash
./bin/start-all.sh --with-tests       # run pytest before launch
./bin/start-all.sh --with-validate    # run validate.py before launch
./bin/start-all.sh --with-preflight   # both
WITH_PREFLIGHT=1 ./bin/start-all.sh   # env-var equivalent
```

| Service | URL | Serves |
|---|---|---|
| Public API + dashboard | `http://127.0.0.1:8000` | `/api/v1/*` analyst read API + web UI + `/docs` (OpenAPI) |
| **Admin panel** | `http://127.0.0.1:8001` | `/admin` HTML + `/api/v1/admin/*` (separate process — see below) |
| Jupyter Lab | `http://127.0.0.1:8888` | The narrative notebook |
| HTML docs | `http://127.0.0.1:8765` | Standalone documentation site |

The admin panel is a **separate FastAPI process** (`api.admin_app`) on its
own port. The split exists so production deploys can route admin traffic
through a private listener (Gateway API HTTPRoute, VPN, IAM-gated path)
while the public API stays behind a CDN. Both processes share the admin
DB; settings changes propagate via Postgres `LISTEN/NOTIFY` (or the 5 s
TTL cache fallback on SQLite). See [ADR 0014 §"Control plane vs. data
plane"](docs/adr/0014-cloud-agnostic-split-plane-architecture.md) and
[`deploy/k8s/gateway.yaml`](deploy/k8s/gateway.yaml).

Run the admin panel alone (without the rest):

```bash
make admin                       # uvicorn api.admin_app:app on :8001
ADMIN_PORT=9001 make admin       # different port
```

`Ctrl+C` traps cleanly and stops everything (recursive process-tree cleanup). Logs accumulate under `.run-logs/`. Override ports via env vars: `API_PORT=9000 ADMIN_PORT=9001 ./bin/start-all.sh`.

### Container alternative (docker compose)

```bash
make compose-up                  # docker compose up --build -d
make compose-down                # docker compose down
docker compose --profile proxy up -d   # with Caddy reverse proxy on :80
```

## Production readiness

### Security
| Concern | How it's handled |
|---|---|
| **API key auth** | `X-API-Key` header check on every `/api/v1/*` route. Disabled when `auth.api_key` runtime setting is empty (dev). Health probe stays public. |
| **Admin login brute-force guard** | Stricter 5/min/IP rate limit on `/api/v1/admin/login` and `/api/v1/admin/password` via a dedicated FastAPI dependency (`strict_rate_limit`), layered on top of the global slowapi cap. |
| **Body-size cap (DoS guard)** | `BodySizeLimitMiddleware` rejects requests >1 MiB with a 413 envelope before any handler allocates buffers. Checks `Content-Length` first, then enforces the cap on streaming/chunked bodies. |
| **CORS** | Configurable origins (runtime setting). Tighten in prod. |
| **Rate limiting (global)** | `slowapi` with default 120 req/min/IP, `X-RateLimit-*` headers; admin-tunable. |
| **Security headers** | CSP, HSTS (prod only), X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy — all on every response. |
| **Admin password storage** | PBKDF2-SHA256 (200k iters); HMAC-signed session cookie with `Secure` (prod) + `HttpOnly` + `SameSite=Strict`. |
| **Audit log** | Every admin mutation recorded with actor + before/after value; surfaced in `/admin`. |
| **Request IDs** | Every request stamped with `X-Request-ID`. Honored if inbound (load balancer / mesh propagation). |
| **Error envelope** | All errors return `{"error": {code, message, request_id, path, details?}}` — no framework internals leak. |
| **API versioning** | All routes under `/api/v1/`. Future v2 ships side-by-side without breaking clients. |
| **CI security scans** | `pip-audit` (dependencies), `trivy` (filesystem + image), `bandit` (Python SAST). |
| **Disclosure process** | See [`SECURITY.md`](SECURITY.md). |

### Performance & caching
| Concern | How it's handled |
|---|---|
| **Response compression** | `GZipMiddleware` (min 500 bytes, level 6). Typical `/api/v1/meetings` payload: 20 KB → 3.6 KB on the wire (5.5×). |
| **HTTP cache (ETag)** | Read endpoints return `ETag` + `Cache-Control: max-age=60`; clients revalidate cheaply via `If-None-Match` → 304 Not Modified (no body). |
| **Pinned dependencies** | Frontend CDN scripts (Plotly, Mermaid) pinned to specific versions with **Subresource Integrity (SRI)** — browsers refuse tampered bytes. |
| **Load-tested baseline** | `make load-test` — 11 endpoints, weighted traffic mix. Local reference: ~395 RPS, p95 ≤ 35ms. |

### Resilience
| Concern | How it's handled |
|---|---|
| **Graceful degradation on refresh failure** | Pipeline refresh runs in the background. If it fails, the API keeps serving the last-good state. Every `/api/*` response carries `X-State-Age-Seconds`; once the data is older than 2× the refresh interval and refresh is failing, `X-Stale-Response: true` is set. Refresh failures stop being user-visible 5xx outages. |
| **Cold-start fix (snapshot)** | When `snapshot.url` is set, replicas read a precomputed `PipelineState` written by a singleton CronJob (`api/snapshot_writer.py`) instead of rebuilding from the data source on every boot. Manifest checksum drives in-place reload. |
| **Backpressure** | `BackpressureMiddleware` caps inflight requests; over the cap returns `503 + Retry-After`. The LB sheds load instead of escalating it. |
| **Circuit breakers** | `api/circuit_breaker.py` — closed/open/half_open state machine; the readiness probe wraps the Postgres reach in `readiness_db_probe`. Sick downstream → fast 503 instead of pile-up. |
| **Graceful shutdown** | Lifespan flips readiness → 503, sleeps so the LB can observe, drains background tasks, disposes the DB pool. Pod terminations don't drop in-flight work. |
| **OpenAPI documented** | Every Pydantic response model carries a concrete example payload — the auto-generated `/docs` page is copy-pasteable, not abstract. |
| **Supply-chain artifacts** | CI generates a CycloneDX SBOM (via `syft`) and a license report (`pip-licenses`); the build fails on copyleft licenses incompatible with MIT distribution. |

### Observability (opt-in)
| Concern | How it's handled |
|---|---|
| **Structured logs** | Text or JSON via `LOG_FORMAT`. Each request gets a one-line access log with `request_id`, `method`, `path`, `status`, `elapsed_ms`. |
| **Prometheus metrics** | `/metrics` endpoint with request rate, latency histograms, status codes per route. |
| **OpenTelemetry tracing** | FastAPI auto-instrumented; head trace-sampling rate from `observability.otel_sample_rate`. Tail-based sampling (errors + slow tails + 1% probabilistic) configured in the OTel Collector DaemonSet (`deploy/k8s/otel-collector.yaml`). |
| **Sentry** | Errors auto-forwarded when `SENTRY_DSN` is set. |
| **Probes (split)** | `/api/live` for k8s liveness (process only); `/api/ready` for readiness (pipeline warm, DB reachable via circuit breaker, not draining). `/api/health` retained for backward compatibility. |

### Engineering
| Concern | How it's handled |
|---|---|
| **Packaging** | `pyproject.toml` (PEP 621); installable via `pip install -e ".[dev]"`; entry-point scripts. |
| **Configuration** | Two-tier: `bootstrap.toml` (env, log, DB URL, admin secret, `[runtime]` knobs that need to be readable before the DB exists) + DB-backed `runtime_settings` for everything else (rate limits, risk weights, feature flags, Redis URL, snapshot URL, OTel sampling rate, …). Operator changes propagate via `LISTEN/NOTIFY` on Postgres. See [`bootstrap.toml.example`](bootstrap.toml.example). |
| **Linting / formatting** | `ruff` (lint + format) configured in pyproject. |
| **Type checking** | `mypy` for `src/` and `api/`. |
| **Testing** | `pytest`, **222 tests** (incl. autoscaling, distributed/`fakeredis`, outbox, cache-stampede, adaptive-throttle, secret-type masking), FastAPI `TestClient`. |
| **CI/CD** | GitHub Actions: lint → type-check → test (3.9/3.11/3.12) → security scan → Docker build + image scan. |
| **Containerization** | Multi-stage Dockerfile, non-root user, healthcheck, JSON logs, Caddy reverse proxy via compose. |
| **Pipeline lifecycle** | State cached at startup; optional periodic refresh (`pipeline.refresh_minutes` runtime setting). At production volume, a singleton CronJob writes a snapshot (`api/snapshot_writer.py`); replicas warm from it instead of rebuilding. |
| **Pre-commit** | ruff + mypy + standard hooks (`pre-commit install`). |
| **API contracts** | Pydantic response models + OpenAPI auto-docs at `/docs`. |
| **Documentation** | README + ARCHITECTURE + APPROACH (with Mermaid) + standalone HTML build. |

### Configuration

**No environment variables for application config** — see [ADR 0009](docs/adr/0009-admin-panel-for-runtime-config.md). Two layers instead:

**Bootstrap config** — minimum to start the service. Copy `bootstrap.toml.example` to `bootstrap.toml`, edit:

```toml
[app]
env = "prod"                 # affects defaults like HSTS
log_level = "INFO"
log_format = "json"

[database]
url = "postgresql+psycopg://user:pass@host/dbname"   # or SQLite for dev

[admin]
initial_password = "..."     # used only on first login; rotate via /admin
session_secret = "..."

[observability]
sentry_dsn = "https://..."
otel_endpoint = "http://otel:4318/v1/traces"
```

**Runtime config** — everything else lives in the DB and is managed through the admin panel at **`/admin`**. Categories:

- **auth** — API key, CORS origins
- **rate_limit** — default + strict + per-tenant overrides
- **pipeline** — refresh interval
- **risk** — scoring weights + tier thresholds
- **sentiment** — friction-pivot threshold
- **feature** — feature flags (metrics, traces)
- **observability** — Sentry sample rate, OTel head-sample rate
- **backpressure** — inflight cap
- **snapshot** — shared snapshot URL + poll interval (cold-start fix)
- **distribution** — Redis URL for cluster-wide rate limiting + queue
- **llm** — Tier-1 vLLM endpoint, Tier-2 frontier provider/model/budget/timeout, **Tier-2 API key (masked-on-read `secret` type)**

Changes propagate within 5 seconds (or < 100 ms with Postgres `LISTEN/NOTIFY`). Every change is recorded in an audit log; `secret`-typed values store their masked form in the audit row so the raw key never persists in the historical record.

### Admin panel

The admin panel is a **separate FastAPI process** on its own port (default `:8001`). Production deploys route admin traffic through a private listener while the public API stays behind the CDN. See ADR 0014 §"Control plane vs. data plane" and `deploy/k8s/gateway.yaml`.

```bash
make admin                                # uvicorn api.admin_app:app on :8001
open http://127.0.0.1:8001/
# Initial password: from [admin].initial_password in bootstrap.toml
```

Or as part of the full dev stack via `./bin/start-all.sh` (also brings up the public API, Jupyter, and docs server).

Three tabs:
- **Settings** — categorized rows with inline edit; saves on blur, "Reset to default" per row. The `secret` type renders as a password input with a masked placeholder (`••••••<last 4>`); typing rotates the key, leaving it blank keeps the current value.
- **Audit log** — append-only history of every change (who, when, old value, new value, notes). Secret values appear in their masked form — the raw key is not recoverable from the log.
- **Account** — password rotation

### What's deliberately not done

- **Multi-tenant auth** — single API key today; JWT is a one-line dependency swap if needed
- **Database persistence** — dataset is static JSON; pipeline runs in 10s
- **Async I/O refactor** — not a bottleneck at this scale; sync handlers are simpler
- **Distributed cache** — single-instance singleton suffices; Redis is the next step at scale

## Auto-scaling at production volume

A QLoRA fine-tune of Gemma 4 was completed during development to validate the recipe and the cost economics; production runs the same trainer logic on a multi-node cluster with autoscaled inference. Each layer scales independently against its own bottleneck signal and goes to zero when idle.

```mermaid
flowchart LR
    Kafka[(Kafka<br/>transcripts)] --> Data["📥 Ray Data<br/>0..N CPU on lag"]
    Data --> Iceberg[(Iceberg<br/>training_sets)]
    Iceberg --> Train["🏋️ Ray Train + FSDP<br/>0..N H100 spot"]
    Train --> S3[(S3<br/>LoRA adapters)]
    S3 --> vLLM["⚡ vLLM<br/>multi-LoRA, HPA<br/>min 1 pod, max 24"]
    vLLM --> Inf[Production inferences]
    Inf --> Judge["♻️ LLM-as-judge<br/>active learning"]
    Judge --> Iceberg

    classDef store fill:#e3f2fd
    classDef compute fill:#e8f5e9
    class Kafka,Iceberg,S3 store
    class Data,Train,vLLM,Judge compute
```

Architecture, cost math (~$50–100/day at typical load), code skeletons, and K8s manifests live in [ADR 0010](docs/adr/0010-auto-scaling-ml-pipeline.md) + [`gemma-finetune/scaling/`](gemma-finetune/scaling/README.md).

## Edge-case test data

We ship 15 hand-crafted synthetic meetings covering corners a production stream is unlikely to surface organically (lowercase URGENT prefix, unicode customer names, net-new product references, all-neutral long meetings, single-sentence inputs, multi-incident scenarios, historical-incident references, internal meetings that mention customers).

```bash
make gen-synthetic              # (re)generate the fixtures
make validate-edge              # run the 10 audits over the union of real + synthetic
```

The synthetic fixtures already caught 2 real categorizer bugs during development (compound `ESCALATION URGENT:` prefix; hyphenated customer names like "Foo-Bar Industries" being truncated at the internal hyphen). Both are fixed; both are now regression-protected by `tests/test_edge_cases.py`. See [`docs/edge-cases.md`](docs/edge-cases.md) for the matrix of which edge cases need synthetic data, which are better tested as units, and which are deferred until they materialize.

## Documentation

- [`docs/APPROACH.md`](docs/APPROACH.md) — methodology, comparisons, and verdicts (start here)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design with Mermaid diagrams
- [`docs/edge-cases.md`](docs/edge-cases.md) — synthetic data strategy + the test matrix
- [`docs/adr/`](docs/adr/) — Architecture Decision Records (immutable, dated, per-decision)
- **`docs/html/`** — same content as standalone HTML files. `make docs` to build, `open docs/html/index.html` to view
- [`docs/presentation.html`](docs/presentation.html) — multi-slide HTML presentation (13 slides, keyboard-navigable). Run `make slides` or open the file directly.
- [`research/deep-research-report.md`](research/deep-research-report.md) — independent cloud-agnostic blueprint adopted as the reference architecture (ADR 0014).
- [`deploy/slos.md`](deploy/slos.md), [`deploy/dr-runbook.md`](deploy/dr-runbook.md) — operational artifacts: SLO catalogue and disaster-recovery procedures.

### Load testing

```bash
make dev               # start the API on :8000
make load-test         # 30s, 20 VUs, all endpoints — exits non-zero if error rate >1%
```

Reference numbers from a local Mac (single uvicorn worker): **~395 RPS, p95 ≤ 35ms, 0% errors** across the 11-endpoint mix. The script lives in [`tests/load/run_load_test.py`](tests/load/run_load_test.py) — vanilla Python (httpx + asyncio), no external tool dependency.

## License

[MIT](LICENSE)
