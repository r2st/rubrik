# Architecture

This document explains how the system is organized, how data flows through it, and how the three interfaces (notebook, dashboard, CLI) share a single analytical core.

## Contents

- [Design principles](#design-principles)
- [System overview](#system-overview)
- [Module dependencies](#module-dependencies)
- [Data model](#data-model)
- [Pipeline stages](#pipeline-stages)
- [Interface layering](#interface-layering)
- [Validation flow](#validation-flow)
- [Caching & performance](#caching--performance)

---

## Design principles

1. **Single source of truth.** All categorization rules, thresholds, and keyword lists live in `src/config.py`. Analysis modules read from config — they never hardcode their own values.
2. **Pure functions where possible.** Each insight module is a function: takes DataFrames in, returns a DataFrame out. Composable, testable, dashboard-friendly.
3. **Three interfaces, one core.** The notebook, Streamlit dashboard, and CLI runner all import from `src/`. No duplicated logic.
4. **Rules + ML, not rules vs. ML.** Regex for high-structure decisions (call type, product area). TF-IDF/KMeans for latent themes. The two layers are complementary, not competing.
5. **Validate semantically, not just syntactically.** Unit tests verify rule behavior; `validate.py` audits the rules against the actual dataset.

---

## System overview

```mermaid
flowchart LR
    subgraph "Input"
        Data[("100 meeting<br/>directories<br/>(JSON × 6)")]
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
        DB["📊 Dashboard<br/>(Streamlit)"]
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

**Reading this diagram:** raw JSON flows in from the left, gets parsed into typed DataFrames, then enriched in three parallel passes (categorize, sentiment, cluster). Insight modules consume those enriched DataFrames. The four interfaces on the right all draw from the same insight functions — there is no logic duplicated across them.

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

    classDef root fill:#1f77b4,color:#fff
    classDef leaf fill:#aec7e8
    class config root
    class viz leaf
```

`config.py` is the dependency root — every other module reads from it. `visualizations.py` is the leaf — nothing imports from it. This is a clean DAG; no cycles.

---

## Data model

A meeting directory contains six JSON files. We project them into three tabular shapes for analysis.

```mermaid
erDiagram
    MEETING ||--o{ SENTENCE : has
    MEETING ||--o{ SPEAKER_SEGMENT : has
    MEETING ||--o{ ACTION_ITEM : has
    MEETING ||--o{ KEY_MOMENT : has
    MEETING ||--o{ EVENT : has

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
        string type "churn_signal|technical_issue|concern|positive_pivot"
        string speaker
        float time_offset
    }
```

`MEETING` is the analysis-ready row. The categorizer adds `call_type`, `meeting_purpose`, `product_areas`, `customer`. The sentiment module adds `trajectory`, `max_drop`, `share_negative`. The clustering module adds `content_cluster`.

---

## Pipeline stages

```mermaid
flowchart TD
    Start([Start]) --> S1[1\. Load all meetings<br/>data_loader.load_all_meetings]
    S1 --> S2[2\. Project to DataFrames<br/>meetings_to_dataframe<br/>sentences_dataframe<br/>speakers_dataframe]
    S2 --> S3[3\. Categorize<br/>regex rules → call_type, purpose,<br/>product_areas, customer]
    S3 --> S4[4\. Sentiment trajectories<br/>bucket sentences × 5 → trajectory,<br/>max_drop, share_negative]
    S4 --> S5[5\. Cluster content<br/>TF-IDF → KMeans<br/>k chosen via silhouette]
    S5 --> S6[6\. Run insights<br/>customer_health · incident_impact ·<br/>action_item_load · competitive_signals ·<br/>speaker_dominance · negative_pivots]
    S6 --> S7[7\. Render visualizations<br/>10 PNGs]
    S7 --> S8[8\. Export<br/>CSV · JSON]
    S8 --> End([Done])

    style S3 fill:#e3f2fd
    style S4 fill:#e3f2fd
    style S5 fill:#e3f2fd
    style S6 fill:#fff3e0
```

Stages 3–5 run in series in `run_analysis.py` but are mutually independent on data — they could be parallelized. Stage 6 depends on all three enrichments being present.

---

## Interface layering

```mermaid
flowchart TB
    subgraph "User interfaces"
        UI1["transcript_intelligence.ipynb<br/>(narrative, panel-ready)"]
        UI2["dashboard.py<br/>(Streamlit interactive)"]
        UI3["run_analysis.py<br/>(batch / CI)"]
        UI4["validate.py<br/>(audit)"]
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

Every interface uses the same public API. If you change `categorizer.annotate`, all four interfaces pick it up automatically. If you add a new insight, you import it in whichever interface needs it — no scaffolding.

---

## Validation flow

```mermaid
flowchart LR
    Data[(Dataset)] --> Pipeline[Pipeline]
    Pipeline --> Audit{validate.py}

    Audit --> C1["Rule coverage<br/>(catch-all bucket size)"]
    Audit --> C2["Customer extraction<br/>(every external has a customer?)"]
    Audit --> C3["Product cross-reference<br/>(rules ↔ dataset's topics field)"]
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

Each check is a small function returning a `Check(name, status, detail)`. Adding a new check is one new function and one line in `main()`.

---

## Caching & performance

| Layer | Cache | Reason |
|---|---|---|
| Streamlit dashboard | `@st.cache_data` on `load_pipeline()` | Pipeline runs once per session, not on every widget change |
| Notebook | none | Re-running cells is the user's intent |
| CLI | none | Designed for one-shot batch |

Pipeline runtime: ~10 seconds end-to-end on 100 meetings. The bottleneck is silhouette-based `k` selection (fits 7 KMeans models). At 10x scale, switch to mini-batch KMeans or run the silhouette sweep on a sample.

---

## Extensibility checklist

- **Add a new insight:** new function in `insights.py` taking `df` → returning a DataFrame. Wire into `run_analysis.py`, the notebook, the dashboard.
- **Add a new categorization rule:** edit `config.PURPOSE_RULES` or `config.PRODUCT_KEYWORDS`. Add a test in `tests/test_categorizer.py`. No analysis code touched.
- **Add a new check to validation:** new function in `validate.py` returning `Check(...)`. One new line in `main()`.
- **Add a new visualization:** new `plot_*` function in `visualizations.py`. Call from notebook or CLI runner.
