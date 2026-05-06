# ADR 0001: Record Architecture Decisions

- **Status:** Accepted
- **Date:** 2026-04-12

## Context

Significant design decisions in this project (categorization approach, summarization architecture, framework choice, persistence model, auth model) all involve trade-offs. Without a record, future engineers — including future-me — re-litigate every choice from scratch when the codebase grows.

`docs/APPROACH.md` already explains the *current* state of decisions, but it's a living document that gets edited. We need an immutable, append-only record of decisions *as they were made*, with the context that informed them.

## Decision

We adopt **Architecture Decision Records (ADRs)** in a lightly-adapted [Nygard format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions).

- ADRs live in `docs/adr/NNNN-kebab-case-title.md`
- Each ADR has four sections: **Status**, **Context**, **Decision**, **Consequences**
- ADRs are **immutable once accepted** — superseded by newer ADRs that link back
- Index maintained in `docs/adr/README.md`
- A new ADR is written when a choice is hard to reverse, when multiple options were considered, or when the *why* is non-obvious

## Consequences

**Positive**
- Decisions become defensible in code review, interviews, and team handoffs
- New team members onboard faster — read the ADRs, understand the design
- Architectural drift is detectable: if the code diverges from an ADR, either the code is wrong or a new ADR is needed
- Existing reasoning in `APPROACH.md` and `ARCHITECTURE.md` gets a stable, citable backing

**Negative**
- Modest discipline cost: 15–30 minutes to write a good ADR
- Risk of stale ADRs that haven't been formally superseded — mitigated by the "superseded by" link convention

**Neutral**
- ADRs are a discipline, not a tool — no CI enforcement (yet)
