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
    subgraph Sources["Data sources"]
        Sample[("Client sample<br/>(JSON × 6 per meeting)")]
        DBSrc[("Postgres + Iceberg<br/>(production · ADR 0008)")]
        Stream[("Kafka stream<br/>(real-time · future)")]
    end

    subgraph Ingest["Ingestion (src/)"]
        direction TB
        Repo{{"TranscriptRepository<br/>(Protocol)"}}
        Local["LocalDirectoryRepository<br/>📂 sample / dev"]
        DBRepo["DatabaseRepository<br/>🗄️ slot — ADR 0011"]
        KStream["KafkaStreamingRepository<br/>🌊 slot"]
        Loader["data_loader<br/>📄 JSON → Meeting"]
        Stream2["streaming.py<br/>🔁 mergeable folds"]
        Repo --> Local
        Repo --> DBRepo
        Repo --> KStream
        Local --> Loader
        Loader --> Stream2
    end

    subgraph Core["Core (src/)"]
        direction TB
        Cat["categorizer<br/>🏷️ rules"]
        Sent["sentiment<br/>📈 trajectories"]
        Clust["clustering<br/>🎯 TF-IDF + KMeans"]
        Ins["insights<br/>💡 6 modules"]
        Viz["visualizations<br/>📊 matplotlib"]
        Cfg[("config<br/>⚙️ keywords<br/>thresholds")]
    end

    subgraph Interfaces["Interfaces"]
        NB["📓 Notebook"]
        DB["📊 Dashboard<br/>(FastAPI + Plotly.js)"]
        CLI["⚙️ run_analysis.py"]
        Val["🔍 validate.py"]
    end

    subgraph OutputG["Output"]
        Out[("output/<br/>CSVs · JSON · PNGs")]
    end

    Sample --> Local
    DBSrc -.-> DBRepo
    Stream -.-> KStream

    Loader --> Cat
    Loader --> Sent
    Loader --> Clust
    Stream2 --> Ins
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

    classDef slot stroke-dasharray: 5 5,fill:#f5f7fa,color:#0d2235
    class DBRepo,KStream slot
```

Raw inputs enter on the left and pass through the **ingestion layer** — a `TranscriptRepository` Protocol with one concrete backend today (`LocalDirectoryRepository`, reading the client sample from disk) and two slots wired through the same interface (`DatabaseRepository` for ADR 0008's Postgres + Iceberg, `KafkaStreamingRepository` for real-time). `data_loader` parses each meeting into a typed `Meeting`, and `streaming.py` exposes a mergeable fold so the same pipeline runs at production volume. The core then enriches the data in three parallel passes (categorize · sentiment · cluster), insight modules consume the enriched frames, and four interfaces draw from the same insight functions — no logic duplicated across them.

The next four diagrams unpack this picture along orthogonal axes: **layers** (what each tier owns), **per-meeting flow** (how a single record is enriched), **runtime topology** (what processes exist and what they share), and **control vs. data plane** (operator surfaces vs. analyst surfaces).

### Layered view — what lives at each tier

The system is a five-tier stack. Each tier has one job and depends only on the tiers below it. Two cross-cutting capabilities (configuration + observability) thread through every tier.

```mermaid
flowchart TB
    subgraph L5["🧑‍💻 Presentation"]
        UI["Web dashboard<br/>(static HTML + Plotly.js)"]
        AdminUI["Admin panel<br/>(/admin)"]
        Notebook["Jupyter notebook"]
    end
    subgraph L4["🌐 API · /api/v1/*"]
        Routes["FastAPI routers<br/>(public · v1 · admin)"]
        MW["Middleware stack<br/>body cap · request-id · CSP · CORS · gzip · slowapi · OTel"]
        State["api/state.py<br/>cached pipeline state · refresh task"]
    end
    subgraph L3["🧠 Analytics core (src/ + gemma-finetune/)"]
        direction TB
        Rules["categorizer<br/>📏 regex rules · config-driven"]
        SentS["sentiment<br/>📈 per-sentence labels<br/>+ numpy trajectory math"]
        ML["clustering<br/>🎯 TF-IDF + KMeans<br/>silhouette-picked k"]
        LLM["LLM cascade<br/>🤖 Tier 1: Gemma 4 fine-tune (bulk)<br/>🌐 Tier 2: frontier API (edge cases)"]
        InsightsB["insights<br/>💡 pandas analytics<br/>(health · incidents · pivots · …)"]
        VizB["visualizations<br/>📊 matplotlib · plotly"]
        Rules --> InsightsB
        SentS --> InsightsB
        ML --> InsightsB
        LLM --> InsightsB
        InsightsB --> VizB
    end
    subgraph L2["🔌 Ingestion (src/)"]
        IngestLayer["TranscriptRepository protocol<br/>data_loader · streaming.py"]
    end
    subgraph L1["💾 App persistence (read + write)"]
        AdminDB[("admin DB<br/>settings · audit_log<br/>SQLite dev · Postgres prod")]
    end
    subgraph Ext["🔗 Upstream data sources (read only)"]
        DataSrc[("client sample (JSON files)<br/>Postgres + Iceberg<br/>Kafka stream")]
    end

    L5 --> L4
    L4 --> L3
    L4 --> AdminDB
    L3 --> L2
    L2 --> Ext

    Cfg2[("⚙️ Config<br/>bootstrap.toml +<br/>runtime settings (DB)")]:::cross
    Obs["📊 Observability<br/>logs · /metrics · OTel · Sentry"]:::cross
    Cfg2 -.-> L2
    Cfg2 -.-> L3
    Cfg2 -.-> L4
    Obs -.-> L3
    Obs -.-> L4

    classDef cross fill:#fffbe6,stroke:#a06b00,color:#3d2a00
```

The enforcement of this layering shows up in the import graph (next section): no module reaches across more than one tier.

### Per-meeting data flow

Following one meeting from raw transcript to dashboard chart:

```mermaid
flowchart LR
    JSON[("meeting_id/<br/>transcript.json<br/>summary.json<br/>action_items.json<br/>+ 3 more")]
    Repo["repository.get(id)<br/>→ Meeting (typed)"]
    DF["DataFrame row<br/>+ sentences_df slice<br/>+ speakers_df slice"]
    Enrich1["categorizer.annotate<br/>→ category · is_escalation · risk_signals"]
    Enrich2["sentiment.add_trajectories<br/>→ open/close sentiment · pivots"]
    Enrich3["clustering.cluster_transcripts<br/>→ content_cluster"]
    Insight["insights.customer_health<br/>insights.incident_impact<br/>insights.competitive_signals · …"]
    Cache["api/state.py<br/>(thread-safe singleton)"]
    Resp["GET /api/v1/meetings/{id}<br/>+ ETag + Cache-Control"]
    Chart["dashboard chart<br/>(Plotly.js)"]

    JSON --> Repo --> DF
    DF --> Enrich1 --> Insight
    DF --> Enrich2 --> Insight
    DF --> Enrich3 --> Insight
    Insight --> Cache --> Resp --> Chart

    classDef raw fill:#e3f2fd,stroke:#1f77b4,color:#0d2235
    classDef enrich fill:#e8f5e9,stroke:#2e7d32,color:#0d2235
    classDef serve fill:#fff3e0,stroke:#a06b00,color:#3d2a00
    class JSON,Repo,DF raw
    class Enrich1,Enrich2,Enrich3,Insight enrich
    class Cache,Resp,Chart serve
```

A single meeting is enriched in three independent passes, fed into the insight functions, and the result is cached process-wide. Subsequent dashboard reads hit the cache + an HTTP `ETag`/`Cache-Control` layer — no recomputation per request.

### Runtime topology — what runs, what's shared

A typical production deployment is a small fixed set of processes plus an observability sidecar surface:

```mermaid
flowchart TB
    subgraph Client["Browser / external clients"]
        BR[Web UI · /admin · API consumers]
    end

    subgraph K8s["Kubernetes pod (replica × N)"]
        UV["uvicorn worker<br/>FastAPI app<br/>(reads PipelineState cache)"]
        RT["Refresh task<br/>asyncio · per-process"]
        UV -.-> RT
    end

    subgraph Persist["App persistence (read + write)"]
        AdminDB[("admin DB<br/>SQLite (dev) ·<br/>Postgres (prod)")]
    end

    subgraph SrcG["Upstream data (read only)"]
        Source[("transcript source<br/>filesystem · Iceberg ·<br/>Kafka")]
    end

    subgraph Obs["Observability backends (opt-in)"]
        Prom[("Prometheus<br/>scrapes /metrics")]
        Tempo[("Tempo / Jaeger<br/>OTLP traces")]
        Sentry[("Sentry<br/>exceptions")]
    end

    BR -->|HTTPS| UV
    UV -->|reads + writes| AdminDB
    UV -->|reads| Source
    UV -->|exposes /metrics| Prom
    UV -->|OTLP| Tempo
    UV -->|errors| Sentry

    classDef pod fill:#e3f2fd,stroke:#1f77b4,color:#0d2235
    classDef store fill:#fffbe6,stroke:#a06b00,color:#3d2a00
    class UV,RT pod
    class AdminDB,Source store
```

Each replica owns a private in-memory `PipelineState` cache and a private refresh task. State is process-local on purpose — there's no Redis dependency for the analytical cache; replicas converge on each refresh tick. The shared persistence boundary is the admin DB (settings + audit log) and the transcript source.

### Control plane vs. data plane

The system has two distinct surfaces that share infrastructure but serve different audiences:

```mermaid
flowchart LR
    subgraph CP["🛠️ Control plane — operators"]
        AdminLogin["POST /admin/login<br/>(strict 5/min/IP)"]
        AdminAPI["/api/v1/admin/*<br/>(session-gated)"]
        Settings[("settings table<br/>14 runtime keys")]
        Audit[("audit_log<br/>append-only")]
        AdminLogin --> AdminAPI
        AdminAPI -->|read/write| Settings
        AdminAPI -->|append| Audit
    end

    subgraph DP["📈 Data plane — analysts / consumers"]
        PubAPI["/api/v1/* read APIs"]
        Health["/api/health (no auth)"]
        Pipeline["Pipeline cache<br/>(api/state.py)"]
        PubAPI --> Pipeline
    end

    Settings -->|5s TTL cache<br/>tunes auth, rate limits, weights| PubAPI
    Settings -.->|tunes| Pipeline
```

Operators tune behavior in the **control plane** (rate limits, churn weights, the API key, feature flags) and every change is audited. Analysts consume the **data plane** through versioned read APIs whose behavior is parameterized by the control plane's current settings — so an operator can adjust risk thresholds without a deploy, and the change propagates to every replica within 5 seconds (the runtime-settings cache TTL).

### Analytics core — what each algorithm does

The "Analytics core" tier in the layered view is intentionally heterogeneous: rules, classical ML, and a fine-tuned LLM each handle the work they're best at. ADR 0002 explains why this hybrid beats any one approach taken to the limit; the diagram below pins what's where.

```mermaid
flowchart LR
    subgraph IN["Inputs"]
        Title["meeting title"]
        Body["full transcript<br/>(joined sentences)"]
        Sents["per-sentence rows<br/>(with sentimentType)"]
        Gold["gold summaries +<br/>action_items.json"]
    end

    subgraph Rules["📏 Rule layer · src/categorizer.py"]
        Cat1["classify_call_type()<br/>regex on title"]
        Cat2["classify_purpose()<br/>ordered regex cascade"]
        Cat3["detect_product_areas()<br/>keyword bag (multi-label)"]
        Cat4["extract_customer()<br/>regex group capture"]
    end

    subgraph Stat["🎯 Classical ML · src/clustering.py + src/sentiment.py"]
        TFIDF["TfidfVectorizer<br/>(stop words · ngram_range)"]
        KMeans["KMeans (k chosen by<br/>silhouette over 4–10)"]
        Traj["numpy trajectory math<br/>open/close means · pivots ·<br/>per-speaker dominance"]
    end

    subgraph LLMt["🤖 LLM tier · gemma-finetune/"]
        Base["Gemma 4 E4B-it base"]
        QLoRA["QLoRA fine-tune<br/>(r=16, α=32, 3 epochs)"]
        Adapter["v3-e4b-allrec adapter<br/>(ROUGE-L 0.394)"]
        Serve["vLLM serve + LoRA hot-swap<br/>(prod path · ADR 0010)"]
        Base --> QLoRA --> Adapter --> Serve
    end

    subgraph Out["📤 Insight outputs"]
        Health["customer_health<br/>risk tiers"]
        Incident["incident_impact"]
        Compete["competitive_signals"]
        Pivots["negative_pivots"]
        Sum["meeting summary +<br/>action items (LLM)"]
    end

    Title --> Cat1
    Title --> Cat2
    Title --> Cat4
    Body --> Cat3
    Body --> TFIDF --> KMeans
    Sents --> Traj
    Gold --> QLoRA
    Body --> Serve

    Cat1 --> Health
    Cat2 --> Incident
    Cat3 --> Compete
    Cat4 --> Health
    KMeans --> Compete
    Traj --> Pivots
    Traj --> Health
    Serve --> Sum

    classDef rules fill:#e3f2fd,stroke:#1f77b4,color:#0d2235
    classDef stat fill:#e8f5e9,stroke:#2e7d32,color:#0d2235
    classDef llm fill:#f3e5f5,stroke:#6a1b9a,color:#1a0628
    class Cat1,Cat2,Cat3,Cat4 rules
    class TFIDF,KMeans,Traj stat
    class Base,QLoRA,Adapter,Serve llm
```

| Layer | Algorithm | Where | Why this layer | Cost / latency |
|---|---|---|---|---|
| **Rules** | Compiled regex + keyword bags, config-driven | `src/categorizer.py` (rules in `src/config.py`) | Call types and purposes follow strict prefixes (`Support Case #`, `Aegis /`, `URGENT:`) — rules cover ~90% with sub-ms inference and full auditability. | <1 ms · free · deterministic |
| **Sentiment** | Per-sentence labels (from source data) + numpy trajectory math | `src/sentiment.py` | Trajectories surface mid-meeting pivots that an averaged score hides. Pure numeric work — no model needed. | <10 ms / meeting · free |
| **Classical ML** | TF-IDF (sklearn) → KMeans, `k` chosen by silhouette over 4–10 | `src/clustering.py` | Catches latent cross-cutting themes (multi-product migrations, cost-driven renewals) that the rules' fixed taxonomy can't see. | seconds for sample; MiniBatchKMeans / Spark MLlib at 10M+ (ADR 0008) |
| **LLM (fine-tuned)** | Gemma 4 E4B-it + QLoRA adapter (`v3-e4b-allrec`, ROUGE-L 0.394) | `gemma-finetune/` adapters; production path served via vLLM with multi-tenant LoRA hot-swap (ADR 0010) | Generative tasks where rules and clustering can't compete: meeting summary in client house style + structured action-item extraction. | ~150 ms / meeting on H100 vLLM; $1.40 to train v3 on the sample |
| **Insights** | Pandas joins, weighted scoring, threshold logic | `src/insights.py` | Composes the four signal layers above into business-readable outputs (customer health, incident impact, competitive mentions, negative pivots). | <100 ms / meeting |

**No LLM in the categorization path.** ADR 0002 documents the deliberate choice: zero-shot LLM matched the rules' accuracy at the sample's structure but added $1–$10/1k-doc cost, 0.5–3s latency, non-determinism, and a data-egress surface — none of which were worth paying for a problem rules already solve. The LLM earns its cost on the *generative* tasks (summaries + action items), not the classification ones.

The training pipeline for the LLM tier (Ray Data dataset prep → multi-node FSDP fine-tune → adapter registry → vLLM serving with autoscaled GPU pools → active-learning feedback loop) is a separate auto-scaling architecture; see ADR 0010 and the "Auto-scaling ML pipeline" section below.

#### LLM cascade — fine-tuned for bulk, frontier model for edge cases

The fine-tuned Gemma 4 adapter is the right tool for the **bulk** of generative work — in-distribution summaries and action items where it's cheap, fast, and self-hosted. It's the wrong tool for **edge cases**: out-of-distribution meetings (new product domains, languages), long-context reasoning across an account's history, world-knowledge-dependent comparisons, and high-stakes outputs that warrant a second opinion. For those, we route to a frontier model (Claude / GPT-4 / Gemini Pro) as Tier 2.

```mermaid
flowchart LR
    In["meeting input"] --> Rules["📏 Rules<br/>categorization · extraction"]
    Rules --> T1{"Tier 1<br/>fine-tuned Gemma 4<br/>vLLM + LoRA hot-swap"}
    T1 --> Conf{"confidence<br/>signals OK?"}
    Conf -->|yes (~95% of traffic)| Out1["🚀 ship<br/>~150 ms · $0 marginal"]
    Conf -->|no| Guard{"PII redaction +<br/>per-tenant policy<br/>+ daily $ budget"}
    Guard -->|allow| T2["🌐 Tier 2 frontier model<br/>(Claude/GPT-4/Gemini)<br/>via gateway"]
    Guard -->|deny| Fallback["return Tier-1 result<br/>flagged 'low confidence'"]
    T2 --> Cache[("response cache<br/>(input hash → output)")]
    Cache --> Out2["✅ ship<br/>~1–3 s · $0.005–0.05 / call"]
    T2 --> Train[("active-learning queue<br/>→ next Gemma fine-tune")]

    classDef fast fill:#e8f5e9,stroke:#2e7d32,color:#0d2235
    classDef slow fill:#f3e5f5,stroke:#6a1b9a,color:#1a0628
    classDef guard fill:#fffbe6,stroke:#a06b00,color:#3d2a00
    class Rules,T1,Out1 fast
    class T2,Out2 slow
    class Guard,Fallback guard
```

**Escalation triggers** (any one fires → Tier 2):
- Generation perplexity above a tuned threshold (Gemma is uncertain).
- LLM-as-judge score below threshold on the Tier-1 output (we already use this signal for active-learning).
- Out-of-distribution flag — input embedding far from the training distribution centroid.
- Operator-flagged or product-flagged categories (e.g., legal, executive briefs) configured in `runtime_settings`.
- Long-context jobs (>8k tokens of input or multi-meeting joint analysis) — Gemma's context is too short, route directly.

**Guardrails before any external call** — every escalation goes through a gateway that enforces:
- **PII redaction** (regex + spaCy NER) before the payload leaves the perimeter.
- **Per-tenant policy** — customers requiring data residency or no-third-party-LLM are blocked at this hop and get the Tier-1 result flagged "low confidence."
- **Daily $ budget cap** per tenant; over budget → Tier-1 result + alert.
- **Response cache** keyed on `(input hash, prompt version)` — identical inputs don't re-pay.
- **Audit log** entry per call (which model, latency, $ cost, redaction summary).

**Closing the loop with active learning.** Every Tier-2 escalation is a high-signal training example: the production data point Gemma struggled on plus a frontier-model reference output. Those flow into the active-learning queue (ADR 0010) and become the next Gemma fine-tune. Tier-2 traffic share is the headline metric for this loop — when it trends down for a category, Gemma has learned that pattern and the cost decays.

**Why not "frontier-only"?** At the volume envelope this system targets (millions to 100M+ meetings), running every meeting through a frontier API is cost-prohibitive ($1k–$1M/day) and creates a hard third-party dependency on the latency-critical path. The cascade gives ~95% of the cost economics of self-hosting *and* the quality ceiling of frontier models on the 5% that needs it.

> The frontier-LLM gateway is a **planned** addition (the diagram and rationale belong in this doc so the architecture target is unambiguous). It is not yet implemented — see ADR 0012 for the decision record and rollout plan.

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

## Auto-scaling the API tier

ADR 0010 covers the ML pipeline (training + vLLM serving). The **API tier itself** has its own auto-scaling story — and several gaps the current implementation hasn't yet closed. ADR 0013 documents the target; the diagram below shows it.

```mermaid
flowchart TB
    subgraph Edge["🌐 Edge"]
        CDN["CDN (CloudFront/Fastly)<br/>honors ETag + Cache-Control"]
        Envoy["Envoy / API gateway<br/>per-tenant rate limit (Redis)"]
    end

    subgraph Migrate["🛠️ Pre-deploy"]
        MJob["k8s Job: alembic upgrade head<br/>completes before rolling update"]
    end

    subgraph API["📡 API tier (HPA on RPS + p95)"]
        Pod1["uvicorn replica<br/>liveness=/api/live · readiness=/api/ready<br/>concurrency-cap semaphore<br/>circuit breakers (DB · vLLM · frontier)"]
        Pod2["uvicorn replica"]
        PodN["uvicorn replica"]
    end

    subgraph Shared["🔗 Shared state (read-mostly)"]
        Snap[("PipelineState snapshot<br/>S3 / Redis · written by CronJob<br/>replicas read on warm")]
        RTSet[("settings + audit_log<br/>via PgBouncer")]
        RLim[("Redis<br/>cluster-wide rate limit · per-tenant buckets<br/>+ pub/sub for settings invalidation")]
    end

    subgraph Refresh["⏱️ Refresh (singleton)"]
        Cron["k8s CronJob: pipeline rebuild<br/>writes Snap; bumps manifest"]
    end

    subgraph Async["📥 Job queue"]
        Q[("Arq / Celery queue<br/>(scaled by depth)")]
        Workers["worker Deployment<br/>ETL · validate · training-data prep"]
        Q --> Workers
    end

    subgraph Obs["📊 Observability (scaled)"]
        OTC["OTel Collector<br/>tail-sampling (1% + 100% errors)"]
        PromAdapter["Prometheus Adapter<br/>(custom metrics → HPA)"]
    end

    Client["Browser / API consumers"] --> CDN --> Envoy --> Pod1 & Pod2 & PodN
    Pod1 --> Snap
    Pod1 --> RTSet
    Pod1 --> RLim
    Pod1 --> Q
    Cron --> Snap
    RTSet -.->|LISTEN/NOTIFY<br/>or Redis pub/sub| Pod1
    MJob --> RTSet
    Pod1 --> OTC
    OTC --> PromAdapter --> HPA["HPA<br/>scales API replicas"]

    classDef pod fill:#e3f2fd,stroke:#1f77b4,color:#0d2235
    classDef shared fill:#fffbe6,stroke:#a06b00,color:#3d2a00
    classDef job fill:#f3e5f5,stroke:#6a1b9a,color:#1a0628
    class Pod1,Pod2,PodN pod
    class Snap,RTSet,RLim shared
    class Cron,Workers,Q,MJob job
```

The 15 specific improvements (cold-start snapshot, externalized refresh, Redis-backed cluster-wide rate limiting, custom-metric HPA, migrations as a Job, PgBouncer, concurrency-cap backpressure, circuit breakers, settings-change pub/sub, split liveness/readiness, async job queue, CDN, per-tenant fairness, OTel collector + sampling, graceful shutdown) are tabled in [ADR 0013](adr/0013-api-tier-auto-scaling.md). Current code already does some of this (lifespan-based shutdown, ETag headers, slowapi); the rest is the next production-readiness PR.

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
