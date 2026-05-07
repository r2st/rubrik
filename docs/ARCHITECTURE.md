# Architecture

How the system is organized and how data flows through it.

## Design principles

| Principle | What it means in practice |
|---|---|
| **Single source of truth** | All categorization rules, thresholds, and keyword lists live in `src/config.py`. Analysis modules read from config — they never hardcode their own values. |
| **Pure functions where possible** | Each insight module takes DataFrames in, returns a DataFrame out. Composable, testable, dashboard-friendly. |
| **One core, many surfaces** | The notebook, FastAPI dashboard, batch CLI, and validator all import from `src/`. No duplicated logic. |
| **Rules + ML, not rules vs ML** | Regex for high-structure decisions; TF-IDF/KMeans for latent themes; fine-tuned LLM for tasks where rules can't compete. Layered, not opposed. |
| **Validate semantically, not just syntactically** | Unit tests verify rule behavior; `validate.py` audits the rules against the actual dataset. |

---

## System overview

```mermaid
flowchart LR
    subgraph "Input"
        Data[("Client sample<br/>(JSON × 6 per meeting)<br/>scales out → ADR 0008")]
    end

    subgraph "Core (src/)"
        direction TB
        Loader["data_loader<br/>📄 raw → DataFrames"]
        Cat["categorizer<br/>🏷️ rules"]
        Sent["sentiment<br/>📈 trajectories"]
        Clust["clustering<br/>🎯 TF-IDF + KMeans"]
        Ins["insights<br/>💡 6 modules"]
        Viz["visualizations<br/>📊 matplotlib"]
        Cfg[("config<br/>⚙️ keywords<br/>thresholds")]
    end

    subgraph "Interfaces"
        NB["📓 Notebook"]
        DB["📊 Dashboard<br/>(FastAPI + Plotly.js)"]
        CLI["⚙️ run_analysis.py"]
        Val["🔍 validate.py"]
    end

    subgraph "Output"
        Out[("output/<br/>CSVs · JSON · PNGs")]
    end

    Data --> Loader
    Loader --> Cat
    Loader --> Sent
    Loader --> Clust
    Cat --> Ins
    Sent --> Ins
    Clust --> Ins
    Ins --> Viz

    Cfg -.-> Cat
    Cfg -.-> Clust
    Cfg -.-> Ins

    Ins --> NB
    Ins --> DB
    Ins --> CLI
    Ins --> Val
    Viz --> NB
    Viz --> CLI

    CLI --> Out
    NB --> Out
```

Raw JSON enters from the left, gets parsed into typed DataFrames, then enriched in three parallel passes (categorize · sentiment · cluster). Insight modules consume the enriched frames. Four interfaces draw from the same insight functions — no logic duplicated across them.

---

## Module dependencies

```mermaid
graph TD
    config[config.py]
    loader[data_loader.py]
    cat[categorizer.py]
    sent[sentiment.py]
    clust[clustering.py]
    ins[insights.py]
    viz[visualizations.py]

    config --> loader
    config --> cat
    config --> clust
    config --> ins
    config --> viz

    loader --> cat
    loader --> sent
    loader --> clust

    cat --> ins
    sent --> ins

    cat --> viz
    sent --> viz
    clust --> viz
    ins --> viz

    classDef root fill:#1f77b4,stroke:#0d3d66,color:#ffffff
    classDef leaf fill:#aec7e8,stroke:#3a6ea5,color:#0d2235
    classDef mid fill:#f5f7fa,stroke:#5a6b80,color:#0d2235
    class config root
    class viz leaf
    class loader,cat,sent,clust,ins mid
```

`config.py` is the dependency root — every other module reads from it. `visualizations.py` is the leaf. Clean DAG; no cycles.

---

## Data model

A meeting directory contains six JSON files. We project them into three tabular shapes for analysis.

```mermaid
erDiagram
    MEETING ||--o{ SENTENCE : has
    MEETING ||--o{ SPEAKER_SEGMENT : has
    MEETING ||--o{ ACTION_ITEM : has
    MEETING ||--o{ KEY_MOMENT : has

    MEETING {
        string meeting_id PK
        string title
        string call_type "support|external|internal"
        string meeting_purpose "11 categories"
        list product_areas "multi-label"
        string customer "external only"
        datetime start_time
        float duration_min
        float sentiment_score "1..5"
        list trajectory "5 buckets, [-1..1]"
        float max_drop "within-call pivot"
        int content_cluster
    }

    SENTENCE {
        string meeting_id FK
        int index
        string speaker
        string text
        string sentiment "pos|neutral|neg"
        float confidence
        float time_offset
    }

    SPEAKER_SEGMENT {
        string meeting_id FK
        string speaker
        float start_ts
        float end_ts
        float duration
    }

    ACTION_ITEM {
        string meeting_id FK
        string owner
        string text
    }

    KEY_MOMENT {
        string meeting_id FK
        string type "churn_signal|technical|concern|positive_pivot"
        string speaker
        float time_offset
    }
```

`MEETING` is the analysis-ready row. The categorizer adds `call_type`, `meeting_purpose`, `product_areas`, `customer`. The sentiment module adds `trajectory`, `max_drop`, `share_negative`. The clustering module adds `content_cluster`.

---

## Pipeline stages

```mermaid
flowchart TD
    Start([Start]) --> S1["1\. Load all meetings"]
    S1 --> S2["2\. Project to DataFrames"]
    S2 --> S3["3\. Categorize<br/>regex rules → call_type · purpose · product · customer"]
    S3 --> S4["4\. Sentiment trajectories<br/>bucket sentences × 5 → trajectory · max_drop · share_negative"]
    S4 --> S5["5\. Cluster content<br/>TF-IDF → KMeans, k via silhouette"]
    S5 --> S6["6\. Run insights<br/>customer_health · incident_impact · action_item_load · competitive · speaker_dominance · negative_pivots"]
    S6 --> S7["7\. Visualize · Export"]
    S7 --> End([Done])

    style S3 fill:#e3f2fd
    style S4 fill:#e3f2fd
    style S5 fill:#e3f2fd
    style S6 fill:#fff3e0
```

Stages 3–5 run in series in `run_analysis.py` but are independent on data — could be parallelized.

---

## Interface layering

```mermaid
flowchart TB
    subgraph Interfaces
        UI1["📓 transcript_intelligence.ipynb<br/>narrative · panel-ready"]
        UI2["📊 dashboard.py<br/>FastAPI + Plotly.js"]
        UI3["⚙️ run_analysis.py<br/>batch / CI"]
        UI4["🔍 validate.py<br/>audit"]
    end

    subgraph "Public API (src/)"
        API1["data_loader<br/>load_all_meetings()<br/>meetings_to_dataframe()<br/>sentences_dataframe()<br/>speakers_dataframe()"]
        API2["categorizer.annotate(df)"]
        API3["sentiment.add_trajectories(df, sentences)"]
        API4["clustering.cluster_transcripts(texts)"]
        API5["insights.*(df)"]
        API6["visualizations.plot_*(...)"]
    end

    UI1 --> API1
    UI1 --> API2
    UI1 --> API3
    UI1 --> API4
    UI1 --> API5
    UI1 --> API6
    UI2 --> API1
    UI2 --> API2
    UI2 --> API3
    UI2 --> API4
    UI2 --> API5
    UI3 --> API1
    UI3 --> API2
    UI3 --> API3
    UI3 --> API4
    UI3 --> API5
    UI3 --> API6
    UI4 --> API1
    UI4 --> API2
    UI4 --> API3
    UI4 --> API4
    UI4 --> API5
```

Every interface uses the same public API. Change a function in `src/`, every interface picks it up automatically.

---

## Validation flow

```mermaid
flowchart LR
    Data[(Dataset)] --> Pipeline[Pipeline]
    Pipeline --> Audit{validate.py}

    Audit --> C1["Rule coverage<br/>(catch-all bucket size)"]
    Audit --> C2["Customer extraction<br/>(every external has a customer?)"]
    Audit --> C3["Product cross-reference<br/>(rules ↔ topics field)"]
    Audit --> C4["Data quality<br/>(empty transcripts, weird durations)"]
    Audit --> C5["Cluster homogeneity<br/>(redundant w/ rules?)"]
    Audit --> C6["Sentiment alignment<br/>(meeting ↔ sentence)"]
    Audit --> C7["Risk distribution<br/>(threshold calibration)"]

    C1 --> Report[/PASS · WARN · FAIL/]
    C2 --> Report
    C3 --> Report
    C4 --> Report
    C5 --> Report
    C6 --> Report
    C7 --> Report
```

Each check is a small function returning a `Check(name, status, detail)`. Adding a new audit is one new function and one line in `main()`.

---

## API request lifecycle

A request to a `/api/v1/*` endpoint flows through this middleware stack:

```mermaid
flowchart TD
    Req[Incoming request] --> Body["BodySizeLimitMiddleware<br/>reject >1 MiB up front (DoS guard)"]
    Body -->|too large| Err413["413 + error envelope"]
    Body -->|ok| RID["RequestIDMiddleware<br/>mint or honor X-Request-ID<br/>start latency timer"]
    RID --> Sec["SecurityHeadersMiddleware<br/>CSP · HSTS · X-Frame-Options · …"]
    Sec --> CORS["CORSMiddleware<br/>configurable origins"]
    CORS --> GZ["GZipMiddleware<br/>compresses payloads >500B"]
    GZ --> RL["SlowAPI rate limiter<br/>X-RateLimit-* headers"]
    RL --> Strict{"strict_rate_limit dep<br/>(admin login + password only)<br/>5/min/IP"}
    Strict -->|exceeded| Err429["429 + error envelope"]
    Strict -->|ok| OTel["OpenTelemetry<br/>(if OTEL_ENDPOINT set)"]
    OTel --> Auth{X-API-Key check<br/>(if API_KEY set)}
    Auth -->|invalid| Err401["401 + error envelope"]
    Auth -->|ok / disabled| ETag{If-None-Match<br/>matches ETag?}
    ETag -->|yes| NotMod["304 Not Modified<br/>no body, ETag preserved"]
    ETag -->|no| Route["Route handler<br/>reads PipelineState<br/>stamps ETag + Cache-Control"]
    Route --> Stale["StateAgeMiddleware<br/>X-State-Age-Seconds<br/>X-Stale-Response (if applicable)"]
    Stale --> Resp[Response]

    style Body fill:#fff3e0
    style Err413 fill:#ffcdd2
    style RID fill:#e3f2fd
    style Sec fill:#e3f2fd
    style GZ fill:#e8f5e9
    style RL fill:#fff3e0
    style Strict fill:#fff3e0
    style Err429 fill:#ffcdd2
    style Auth fill:#ffebee
    style Err401 fill:#ffcdd2
    style ETag fill:#e8f5e9
    style NotMod fill:#e8f5e9
```

All errors — `HTTPException`, `RequestValidationError`, unhandled — funnel through the same handler in `api/errors.py` and come back as:

```json
{
  "error": {
    "code": "not_found",
    "message": "meeting X not found",
    "request_id": "9574…",
    "path": "/api/v1/meetings/X"
  }
}
```

The `/api/health` endpoint is registered on a separate **public** router that bypasses the auth dependency — load balancers and k8s probes need to hit it without credentials.

---

## Scaling to 100M+ records

The client provided a representative **sample** (currently ~100 meetings, ~4k sentences). The current single-instance, in-memory pipeline is correct **for that sample volume** — it's how we verify pipeline correctness end-to-end during development. At **production volume (millions to 100M+ records)** the substrate changes — the analytical layers run against a real data platform — but the *application code shape* stays mostly the same.

### Scale envelopes per component

| Component | Comfortable up to | Breaks at | Path forward |
|---|---|---|---|
| Regex categorizer | Billions / day on a single thread | n/a — CPU-bound, parallelizes trivially | Already good |
| TF-IDF + KMeans clustering (in-memory) | ~1M docs in-memory | ~10M (RAM ceiling) | Streaming MiniBatchKMeans, then Spark MLlib / Faiss |
| Per-sentence sentiment trajectories | ~1M docs in-memory | ~10M (RAM ceiling) | **Already supported via streaming pipeline** (`src/streaming.py` — `run_analysis.py --streaming`); fold pattern is mergeable so it parallelizes across Ray Data workers |
| Customer health + insight aggregation | ~1M docs in-memory; **unbounded via streaming** | ~10M in-memory groupby | Streaming fold writes per-customer rollup to CSV/Postgres incrementally |
| Pipeline state singleton (`api/state.py`) | ≤ ~100k rows | Memory-bound | DatabaseRepository slot in `src/repository.py` — no call-site changes |
| Repository (data source) | LocalDirectoryRepository: ~100k meetings | Filesystem inode latency | Swap `default_repository()` to return `DatabaseRepository` — Postgres + Iceberg backed |
| FastAPI request layer | Stateless — replicates horizontally | LB / DB-bound | Multi-instance behind LB, Redis cache for hot queries |
| Gemma 4 inference | One pod = ~10 RPS | Pod queue depth | vLLM autoscale → ADR 0010 |
| Schema migrations | Hand-edited DDL in `db.init_db()` | First production schema change | **Alembic now in place** — `alembic upgrade head` |

The point: **every layer has a known ceiling and a known next step**. Nothing requires a rewrite to scale; each transition is a targeted swap with a measurable trigger documented in the relevant ADR.

```mermaid
flowchart LR
    Src[Transcript sources] --> Stream[Kafka / Kinesis<br/>append-only log]
    Stream --> Raw[(S3 + Iceberg<br/>raw + audit)]
    Stream --> Workers[Worker pool<br/>cascaded categorization]

    Workers --> OPDB[(Postgres<br/>operational, 90d)]
    OPDB --> Analytical[(ClickHouse / DuckDB<br/>full history)]
    OPDB --> Search[(OpenSearch<br/>full-text)]
    OPDB --> Cache[(Redis<br/>hot insights)]

    Cache --> API[FastAPI · multi-instance]
    Analytical --> API
    Search --> API
    OPDB --> API
```

**What changes:**
- Pandas in-memory → Postgres canonical + columnar warehouse for analytics
- All-meetings-at-startup → streaming ingestion via Kafka, materialized views
- Single instance → multi-replica behind a load balancer with shared Redis cache

**What doesn't change:**
- Categorization cascade (rules → classifier → LLM) — see ADR 0002
- Sentiment trajectory math — see ADR 0007
- The 6 insight functions — they run against repository interfaces, swappable backend
- The admin panel + runtime settings store — operates on the same Postgres at any scale
- The Gemma 4 fine-tuning **recipe** — only the orchestration layer changes (single H100 → multi-node FSDP via Ray Train; see ADR 0010)

Migration is incremental: each step in [ADR 0008](adr/0008-data-layer-for-scale.md#migration-path-concrete-sequential) is independently shippable, none requires a wholesale rewrite. The current SQLite + SQLAlchemy code is the foundation — change `bootstrap.toml`'s database URL to Postgres and step #2 is done.

---

## Auto-scaling ML pipeline (training + serving)

The Gemma 4 fine-tune in [ADR 0003](adr/0003-self-host-summarization-with-gemma-4.md) was a deliberate proof-of-concept on the client sample (~95 train meetings on a single H100, $1.40 wall-clock cost) — sufficient to demonstrate the recipe works and the economics close. **Production scales every layer independently** without changing the trainer logic:

```mermaid
flowchart LR
    subgraph Data["DATA — Ray Data on KubeRay (autoscale 0..N CPU)"]
        Kafka[Kafka stream] --> Prep[Streaming dedup,<br/>quality, multi-task]
        Prep --> Iceberg[(Iceberg<br/>versioned datasets)]
    end
    subgraph Train["TRAINING — KubeRay + FSDP (autoscale 0..N H100, spot OK)"]
        Iceberg --> Job[Ray Train · 8× H100<br/>FSDP shards 9B+ models]
        Job --> Adapter[(LoRA adapter<br/>~30 MB to S3)]
    end
    subgraph Serve["SERVING — vLLM (autoscale 0..N L4, on-demand)"]
        Adapter --> vLLM[vLLM pod · multi-LoRA hot-swap]
        vLLM --> HPA{HPA on<br/>queue depth +<br/>KV-cache %}
        HPA --> NodePool[Karpenter GPU<br/>NodePool]
    end
    subgraph Loop["ACTIVE LEARNING (autoscale on Kafka lag)"]
        Live[Production inferences] -->|low confidence| Judge[Claude / GPT-4-class<br/>LLM-as-judge]
        Judge -->|labels| Queue[(Training queue)]
        Queue --> Iceberg
    end
    Serve --> Live
```

Each layer scales on the **right signal** and stops at zero when idle:

| Layer | Scales on | Floor | Ceiling |
|---|---|---|---|
| Data prep | Kafka lag | 0 workers | Kafka-bound |
| Training | Job submission | 0 H100 nodes (spot OK) | Per-job request |
| Serving | `vllm_pending_requests` + `vllm_gpu_cache_usage_perc` | 1 always-warm L4 pod | Queue-bound |
| Active learning | Pending labels | 0 workers | Daily LLM-judge budget |

Code skeletons + production K8s manifests live in [`gemma-finetune/scaling/`](../gemma-finetune/scaling/README.md). The single-H100 recipe stays as the local-development entry point; the same trainer logic runs on a 32-GPU cluster via Ray Train. **The recipe doesn't change. The substrate does.**

### Training stack — what's where

```mermaid
flowchart TB
    subgraph K8s["Kubernetes cluster"]
        subgraph CtrlPlane["Control plane · Karpenter cpu-on-demand pool"]
            Job[RayJob CR<br/>shutdownAfterJobFinishes=true]
            Head[Ray head<br/>scheduler · dashboard]
        end
        subgraph TrainPool["Training data plane · Karpenter gpu-h100-spot"]
            W1[Worker 1 · 4× H100<br/>FSDP shard]
            W2[Worker 2 · 4× H100<br/>FSDP shard]
            Wn[… up to 8 nodes]
            W1 -.NCCL/EFA.- W2
            W2 -.NCCL/EFA.- Wn
        end
        Job --> Head
        Head --> W1
        Head --> W2
        Head --> Wn
    end
    Ice[(Iceberg<br/>training_sets)] -.sharded read.-> W1
    Ice -.sharded read.-> W2
    Ice -.sharded read.-> Wn
    W1 --> S3[(S3 LoRA adapters<br/>+ checkpoints)]
    Head --> ML[(MLflow registry)]
```

### Serving stack — autoscaling chain end-to-end

```mermaid
flowchart LR
    Client[Client] -->|/v1/completions<br/>model: tenant-acme| LB[k8s Service]
    LB --> Pod1[vLLM pod 1]
    LB --> Pod2[vLLM pod 2]
    LB --> PodN[…]

    Pod1 -.scrape /metrics.- Prom[Prometheus]
    Pod2 -.scrape.- Prom
    PodN -.scrape.- Prom

    Prom --> Adapter[prometheus-adapter<br/>vllm_pending_requests<br/>vllm_gpu_cache_usage_perc]
    Adapter --> HPA[HPA controller]
    HPA -->|desired replicas| Deploy[gemma-serving<br/>Deployment]
    Deploy -->|pods needed > capacity| Karp[Karpenter]
    Karp -->|provision L4| NodePool[gpu-l4 NodePool<br/>on-demand]
    NodePool -.new node.-> Pod1

    Pod1 -.lazy load.-> S3[(S3 LoRA<br/>tenant adapters)]

    classDef metric fill:#fff3e0
    classDef ctrl fill:#e3f2fd
    classDef pod fill:#e8f5e9
    class Prom,Adapter metric
    class HPA,Deploy,Karp,LB ctrl
    class Pod1,Pod2,PodN pod
```

Two real signals drive scale-up: queue depth (`vllm_pending_requests`) and KV-cache pressure (`vllm_gpu_cache_usage_perc`). CPU% is intentionally not in the loop — it's misleading for GPU inference.

See [ADR 0010](adr/0010-auto-scaling-ml-pipeline.md) for the full architecture, cost math, and decision rationale.

---

## Admin panel — runtime configuration without env vars

Operationally-tunable knobs (rate limits, churn-risk weights, feature flags, the auth API key) live in a database-backed settings store. Operators change them through `/admin` — every change is audited.

```mermaid
flowchart LR
    Boot["bootstrap.toml<br/>(env, log, DB url, admin secret)"] --> App[FastAPI app]
    App --> RuntimeStore[(settings table<br/>admin DB)]
    Browser[Browser] --> Login["POST /api/v1/admin/login"]
    Login -->|signed cookie| Browser
    Browser --> Admin["/admin · settings UI"]
    Admin -->|GET PUT POST| AdminAPI[/api/v1/admin/*]
    AdminAPI --> RuntimeStore
    AdminAPI --> Audit[(audit_log)]
    RuntimeStore -->|5s TTL cache| App
```

What lives where:

| Type of config | Lives in | Examples | Mutable at runtime? |
|---|---|---|---|
| **Bootstrap** | `bootstrap.toml` | env label, log level, DB URL, admin session secret | No (restart required) |
| **Runtime** | DB `settings` table | API key, rate limits, churn weights, feature flags | Yes — change in `/admin`, takes effect within 5s |

**No env vars** are read for application configuration. The runtime substrate (uvicorn, k8s) may still use them for infrastructure. See [ADR 0009](adr/0009-admin-panel-for-runtime-config.md) for the full rationale.

---

## Security hardening

Defense-in-depth controls layered onto the request path:

| Control | Where | Purpose |
|---|---|---|
| `BodySizeLimitMiddleware` (1 MiB) | outermost middleware | Reject oversized requests before any handler allocates buffers — DoS guard. |
| Strict 5/min/IP rate limit | `Depends(strict_rate_limit)` on `/admin/login` + `/admin/password` | Slow brute-force credential attacks below the global slowapi cap. |
| Global slowapi rate limiter | app-wide | Per-IP fairness across all routes; admin-tunable via `rate_limit.default`. |
| PBKDF2-SHA256 (200k iters) | `api/admin/auth.py` | Admin password hashing. |
| HMAC-signed session cookie | `api/admin/auth.py` | `Secure` (prod) + `HttpOnly` + `SameSite=Strict`. |
| `SecurityHeadersMiddleware` | per response | CSP, HSTS (prod), X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy. |
| Audit log | `audit_log` table | Every admin mutation recorded with actor + before/after value. |

Reporting process and scope live in [`SECURITY.md`](../SECURITY.md).

---

## Operational lifecycle

Container start (`Dockerfile` entrypoint):

```mermaid
flowchart LR
    Start([Container start]) --> Boot["read bootstrap.toml<br/>(env, DB url, secrets)"]
    Boot --> Mig["alembic upgrade head<br/>(idempotent; locks safely<br/>under multi-replica boot)"]
    Mig --> Seed["initialize_db_and_seed()<br/>+ ensure_admin_password_seeded()"]
    Seed --> Warm["state.get_state()<br/>warm pipeline cache"]
    Warm --> Refresh["start refresh task<br/>(if interval > 0)"]
    Refresh --> Ready([uvicorn ready])
```

Schema evolution flows through Alembic — `db.init_db()`'s create-all is now an idempotent fallback for sample-volume tests; production-equivalent containers always run migrations on boot.

---

## Performance & caching

| Layer | Cache | Reason |
|---|---|---|
| Streamlit / FastAPI | Pipeline state cached at startup (thread-safe singleton) | Pipeline runs once per process, not per request |
| Notebook | None | Re-running cells is the user's intent |
| CLI | None | Designed for one-shot batch |

End-to-end runtime: ~10s on the client's sample dataset. The bottleneck is silhouette-based `k` selection (fits 7 KMeans models). At ~10× the sample size the silhouette sweep should run on a sample, not the full set; at ~100× MiniBatchKMeans replaces KMeans; beyond that the clustering is out-of-process via Spark MLlib (see ADR 0008's analytical tier).

---

## Extensibility

How to add new things without touching unrelated code:

| Add a new… | Steps |
|---|---|
| **Insight** | New function in `insights.py` taking `df` → returning a DataFrame. Wire into `run_analysis.py`, the notebook, and the dashboard. |
| **Categorization rule** | Edit `config.PURPOSE_RULES` or `config.PRODUCT_KEYWORDS`. Add a test in `tests/test_categorizer.py`. No analysis code touched. |
| **Validation check** | New function in `validate.py` returning `Check(...)`. One new line in `main()`. |
| **Visualization** | New `plot_*` function in `visualizations.py`. Call from notebook or CLI runner. |
| **API endpoint** | New route in `api/routes.py` + Pydantic response model in `api/models.py`. |
