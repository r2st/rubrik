# Transcript Intelligence

> A production-ready pipeline that processes B2B meeting transcripts and surfaces topic categorization, sentiment trends, and strategic insights — exposed as a REST API with a lightweight web dashboard.

[![CI](https://img.shields.io/badge/CI-GitHub%20Actions-blue)](.github/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-123%20passing-brightgreen)](tests/)
[![Coverage](https://img.shields.io/badge/coverage-94%25-brightgreen)](pyproject.toml)
[![Validation](https://img.shields.io/badge/validation-9%2F10%20pass-brightgreen)](validate.py)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

---

## Contents

- [What this does](#what-this-does)
- [Headline findings](#headline-findings)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Testing & validation](#testing--validation)
- [API & web dashboard](#api--web-dashboard)
- [Run all services](#run-all-services)
- [Production readiness](#production-readiness)
- [Documentation](#documentation)

---

## What this does

Given ~100 meeting transcripts (support cases, customer-facing calls, internal meetings), this pipeline:

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

Plus a separate experiment in [`gemma-finetune/`](gemma-finetune/README.md): fine-tunes **Gemma 4 (E4B)** on the dataset's gold summaries to demonstrate a self-hosted alternative to vendor LLM APIs ($1.40 training cost, ROUGE-L 0.39 vs 0.29 baseline). See [APPROACH §Summarization](docs/APPROACH.md#2-summarization--action-items) for the verdict, and [`gemma-finetune/scaling/`](gemma-finetune/scaling/README.md) + [ADR 0010](docs/adr/0010-auto-scaling-ml-pipeline.md) for the production auto-scaling architecture (Ray Train + FSDP for training, vLLM + HPA for serving, active learning for continuous improvement).

## Headline findings

| Area | Headline |
|---|---|
| Categorization | 100 meetings → 3 call types · 11 purposes · 4 product areas. **k=7** content clusters (silhouette-selected). |
| Sentiment | Support 2.94 < internal 3.42 < external 3.71. Detect 3.20 — outage drag. |
| Outage impact | One incident touched **68% of all meetings**, dragged sentiment by **0.77 points**. |
| Top at-risk customers | Northstar Pharma · Cobalt Software · Summit Trust |
| Execution bottleneck | Maria Santos owns 31 action items (most by far) |
| Conversation health | Support calls have **51% single-speaker dominance** — agents may be over-talking |
| Friction moments | **9 meetings** with sharp within-call sentiment drops (sentence-level analysis) |

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

## Architecture

```mermaid
flowchart LR
    Data[("100 meetings<br/>(JSON)")] --> Loader["data_loader<br/>typed DataFrames"]

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
| **Meta** | `GET /api/health` · `GET /api/summary` |
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

What it does:
1. **Pre-flight**: runs the test suite + the semantic validation; aborts on any FAIL
2. **Refreshes** `output/` (batch pipeline) and `docs/html/` (HTML docs) in parallel
3. **Starts** three services in the background, waits for each to be ready, prints the URLs:

| Service | URL | Serves |
|---|---|---|
| FastAPI + dashboard | `http://127.0.0.1:8000` | API + web UI + OpenAPI docs at `/docs` |
| Jupyter Lab | `http://127.0.0.1:8888` | The narrative notebook |
| HTML docs | `http://127.0.0.1:8765` | Standalone documentation site |

`Ctrl+C` traps cleanly and stops everything (recursive process-tree cleanup). Logs accumulate under `.run-logs/`. Override ports via env vars (`API_PORT=9000 ./bin/start-all.sh`); skip pre-flight with `SKIP_PREFLIGHT=1`.

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
| **API key auth** | `X-API-Key` header check on every `/api/v1/*` route. Disabled when `API_KEY` env unset (dev). Health probe stays public. |
| **CORS** | Configurable origins (`CORS_ORIGINS` env). Tighten in prod. |
| **Rate limiting** | `slowapi` with default 120 req/min/IP, `X-RateLimit-*` headers. |
| **Security headers** | CSP, HSTS (prod only), X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy — all on every response. |
| **Request IDs** | Every request stamped with `X-Request-ID`. Honored if inbound (load balancer / mesh propagation). |
| **Error envelope** | All errors return `{"error": {code, message, request_id, path, details?}}` — no framework internals leak. |
| **API versioning** | All routes under `/api/v1/`. Future v2 ships side-by-side without breaking clients. |
| **CI security scans** | `pip-audit` (dependencies), `trivy` (filesystem + image), `bandit` (Python SAST). |

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
| **OpenAPI documented** | Every Pydantic response model carries a concrete example payload — the auto-generated `/docs` page is copy-pasteable, not abstract. |
| **Supply-chain artifacts** | CI generates a CycloneDX SBOM (via `syft`) and a license report (`pip-licenses`); the build fails on copyleft licenses incompatible with MIT distribution. |

### Observability (opt-in)
| Concern | How it's handled |
|---|---|
| **Structured logs** | Text or JSON via `LOG_FORMAT`. Each request gets a one-line access log with `request_id`, `method`, `path`, `status`, `elapsed_ms`. |
| **Prometheus metrics** | `/metrics` endpoint with request rate, latency histograms, status codes per route. |
| **OpenTelemetry tracing** | FastAPI auto-instrumented. OTLP/HTTP export when `OTEL_ENDPOINT` is set. Backend-agnostic (Tempo / Jaeger / Datadog / Honeycomb). |
| **Sentry** | Errors auto-forwarded when `SENTRY_DSN` is set. |
| **Health endpoint** | `/api/health` (no auth) for load balancers and k8s probes. |

### Engineering
| Concern | How it's handled |
|---|---|
| **Packaging** | `pyproject.toml` (PEP 621); installable via `pip install -e ".[dev]"`; entry-point scripts. |
| **Configuration** | `pydantic-settings` reads `.env` + env vars; typed, validated, multi-environment (dev/staging/prod profiles). See [`.env.example`](.env.example). |
| **Linting / formatting** | `ruff` (lint + format) configured in pyproject. |
| **Type checking** | `mypy` for `src/` and `api/`. |
| **Testing** | `pytest`, **86 tests, 94% coverage**, FastAPI `TestClient`. |
| **CI/CD** | GitHub Actions: lint → type-check → test (3.9/3.11/3.12) → security scan → Docker build + image scan. |
| **Containerization** | Multi-stage Dockerfile, non-root user, healthcheck, JSON logs, Caddy reverse proxy via compose. |
| **Pipeline lifecycle** | State cached at startup; optional periodic refresh (`PIPELINE_REFRESH_MINUTES`) when the dataset can change underneath. |
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

**Runtime config** — everything else lives in the DB and is managed through the admin panel at **`/admin`**. Includes:

- Auth (API key, CORS origins)
- Rate limits (default + strict)
- Pipeline refresh interval
- Risk-scoring weights + thresholds
- Sentiment friction threshold
- Feature flags

Changes propagate within 5 seconds. Every change is recorded in an audit log.

### Admin panel

```bash
make dev                    # bring up the API
open http://127.0.0.1:8000/admin
# Initial password: from [admin].initial_password in bootstrap.toml
```

Three tabs:
- **Settings** — categorized rows with inline edit; saves on blur, "Reset to default" per row
- **Audit log** — append-only history of every change (who, when, old value, new value, notes)
- **Account** — password rotation

### What's deliberately not done

- **Multi-tenant auth** — single API key today; JWT is a one-line dependency swap if needed
- **Database persistence** — dataset is static JSON; pipeline runs in 10s
- **Async I/O refactor** — not a bottleneck at this scale; sync handlers are simpler
- **Distributed cache** — single-instance singleton suffices; Redis is the next step at scale

## Auto-scaling at production volume

The Gemma 4 fine-tune (95 meetings, 1× H100, $1.40) is the *workshop recipe*. Production runs the same trainer logic on a multi-node cluster with autoscaled inference. Each layer scales independently against its own bottleneck signal and goes to zero when idle.

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

## Documentation

- [`docs/APPROACH.md`](docs/APPROACH.md) — methodology, comparisons, and verdicts (start here)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design with Mermaid diagrams
- [`docs/adr/`](docs/adr/) — Architecture Decision Records (immutable, dated, per-decision)
- **`docs/html/`** — same content as standalone HTML files. `make docs` to build, `open docs/html/index.html` to view

### Load testing

```bash
make dev               # start the API on :8000
make load-test         # 30s, 20 VUs, all endpoints — exits non-zero if error rate >1%
```

Reference numbers from a local Mac (single uvicorn worker): **~395 RPS, p95 ≤ 35ms, 0% errors** across the 11-endpoint mix. The script lives in [`tests/load/run_load_test.py`](tests/load/run_load_test.py) — vanilla Python (httpx + asyncio), no external tool dependency.

## License

[MIT](LICENSE)
