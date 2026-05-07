# Architecture Decision Records

This directory captures the *why* behind significant design choices. Each ADR is a short, dated, immutable record of a decision and its context.

Format: lightly adapted [Nygard ADR](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions). Each file has Status, Context, Decision, Consequences. Once accepted, ADRs are not edited — they're superseded by newer ADRs that link back.

## Index

| #    | Title | Status | Date |
|------|-------|--------|------|
| 0001 | [Record architecture decisions](0001-record-architecture-decisions.md) | Accepted | 2026-04-12 |
| 0002 | [Hybrid categorization (rules + clustering, not LLM)](0002-hybrid-categorization.md) | Accepted | 2026-04-15 |
| 0003 | [Self-host summarization with Gemma 4 over vendor APIs](0003-self-host-summarization-with-gemma-4.md) | Accepted | 2026-05-05 |
| 0004 | [FastAPI + static frontend over Streamlit](0004-fastapi-over-streamlit.md) | Accepted | 2026-05-05 |
| 0005 | [No database persistence (yet)](0005-no-database-persistence.md) | Accepted | 2026-04-20 |
| 0006 | [API key auth; defer JWT/OAuth](0006-api-key-auth-defer-jwt.md) | Accepted | 2026-05-06 |
| 0007 | [Use sentence-level sentiment trajectories](0007-sentence-level-sentiment-trajectories.md) | Accepted | 2026-04-22 |
| 0008 | [Data layer for 100M+ records — tiered storage](0008-data-layer-for-scale.md) | Accepted | 2026-05-06 |
| 0009 | [Admin panel + runtime config — no env vars for app config](0009-admin-panel-for-runtime-config.md) | Accepted | 2026-05-06 |
| 0010 | [Auto-scaling ML pipeline — data, training, serving](0010-auto-scaling-ml-pipeline.md) | Accepted | 2026-05-06 |
| 0011 | [Repository pattern + streaming pipeline](0011-repository-pattern-and-streaming.md) | Accepted | 2026-05-06 |

## When to write a new ADR

- A choice will be hard to reverse later (storage layer, framework, auth model)
- Multiple options were considered and one was picked for non-obvious reasons
- A future engineer (including future-you) would benefit from knowing *why*

## When NOT to write an ADR

- The choice is mechanical or follows an existing convention
- It's a low-stakes implementation detail
- It's a tactical bug fix
