# ADR 0002: Hybrid Categorization — Rules + Clustering, Not LLM

- **Status:** Accepted
- **Date:** 2026-04-15

## Context

Every meeting in the dataset needs to be categorized along four dimensions: **call type** (support / external / internal), **purpose** (11 categories), **product area** (4 products), and **customer** (for external calls).

The dataset has two kinds of structure:
- **Explicit:** title patterns are highly regular (`Support Case #...`, `Aegis / Customer - ...`, `URGENT: ...`)
- **Latent:** themes that cross structural boundaries (e.g., billing conversations span both support and external calls)

Three approaches were on the table:
1. **Pure regex rules** — fast, free, deterministic, but brittle to title-format change and blind to latent themes
2. **Pure unsupervised clustering** (TF-IDF + KMeans) — finds latent themes but produces clusters that don't map cleanly to business taxonomy
3. **Pure LLM** (zero-shot or fine-tuned) — handles ambiguity well, but costs $1–$10 per 1k docs, has 0.5–3s latency, is non-deterministic, and sends data outside the perimeter

A spike on each:
- Rules labeled 87% of meetings into specific buckets with **99% agreement** with the dataset's own `topics` field
- TF-IDF/KMeans (silhouette-selected `k=7`) surfaces interpretable cross-cutting themes
- Zero-shot LLM matched rules' accuracy but added cost, latency, and non-determinism

## Decision

We ship a **hybrid: regex rules for the explicit layer, TF-IDF + KMeans for the latent layer**. No LLM in the categorization path.

- Rules in `src/categorizer.py`, configuration in `src/config.py`
- Clustering in `src/clustering.py` with silhouette-selected `k`
- The 13% catch-all "Account Management" bucket is *retained* — it's honest signal that some calls don't fit a specific purpose

## Consequences

**Positive**
- Sub-millisecond inference, deterministic, free, fully auditable (one regex per decision)
- 99% agreement with the dataset's own labels validates the approach against an independent ground truth
- Adding a category is a config change + a unit test, not retraining
- No data egress; works in air-gapped environments

**Negative**
- Brittle if title formats change wholesale (acquisition, multi-tenant rename, multilingual data)
- The 13% catch-all bucket is a known coverage gap — `validate.py` monitors it

## When to revisit

- Catch-all bucket grows past 25% (signals title-format drift)
- A customer requires a custom taxonomy that's painful to encode in regex
- We need to support non-English transcripts

At that point, the right move is an **LLM fallback** specifically on the catch-all bucket, not a wholesale rewrite. See ADR 0003 for the analogous summarization decision.

## Related

- `docs/APPROACH.md` § 1 — full comparison matrix
- `tests/test_categorizer.py` — 31 unit tests pinning the rules
- `validate.py` — `check_rule_coverage`, `check_product_cross_reference`
