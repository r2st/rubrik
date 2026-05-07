# Edge Cases & Synthetic Test Data

The system is designed to be auto-scalable to millions / 100M+ records. This doc enumerates corners the production stream is unlikely to surface organically, says **which ones need synthetic data** (vs which are better handled by unit tests or operational hardening), and links to the synthetic fixtures we ship.

---

## Edge cases we care about

### Need synthetic data — the pipeline will see these in prod, organic traffic is unlikely to surface them

| # | Edge case | Why a production stream may not surface it organically | Synthetic fixture |
|---|---|---|---|
| 1 | **Title-format heterogeneity** — lowercase prefix, multi-word URGENT, no separator, multi-`/` (`Aegis / EMEA / Acme`) | Rare lowercase URGENT prefix from ad-hoc tickets; canonical formats dominate organic traffic | [`title_variants.json`](../tests/fixtures/synthetic/title_variants/) |
| 2 | **Edge-character customer names** — apostrophes, accents, hyphens, em-dashes (`L'Oreal`, `Müller GmbH`) | Long-tail unicode names appear infrequently | [`customer_unicode.json`](../tests/fixtures/synthetic/customer_unicode/) |
| 3 | **Net-new product references** — products outside the current keyword list (`DetectPlus`, `ComplyVault`) | New product launches come in bursts after the rules are updated | [`net_new_product/`](../tests/fixtures/synthetic/net_new_product/) |
| 4 | **All-neutral long meeting** — 200-sentence status sync with zero sentiment variation | Real meetings almost always have some sentiment variance | [`all_neutral/`](../tests/fixtures/synthetic/all_neutral/) |
| 5 | **Single-sentence meeting** — degenerate input that breaks naive bucket math | Production meetings are typically much longer | [`single_sentence/`](../tests/fixtures/synthetic/single_sentence/) |
| 6 | **Multi-incident timeline** — two distinct outages referenced in the same meeting | Concurrent incidents on a single call are uncommon | [`multi_incident/`](../tests/fixtures/synthetic/multi_incident/) |
| 7 | **Past-tense incident reference** — outage discussed historically, not currently affecting | Active incidents dominate live traffic | [`historical_incident/`](../tests/fixtures/synthetic/historical_incident/) |
| 8 | **Internal-meeting + customer-name mention** — internal call that happens to discuss a customer | Internal calls rarely mention specific customer names by surface form | [`internal_mentions_customer/`](../tests/fixtures/synthetic/internal_mentions_customer/) |

### Don't need synthetic data — covered better elsewhere

| Edge case | Why synthetic doesn't help | What does |
|---|---|---|
| Empty / zero-sentence meeting | Synthetic JSON would just be a contrived empty file | `tests/test_sentiment.py::test_trajectory_empty_input_safe` |
| Bucket math at sentence count = 0, 1, 5 | Pure math — no transcript needed | Unit tests in `test_sentiment.py` |
| Cluster with k=1 | Math, not data | `test_clustering.py` |
| Concurrent `state.reload()` | Threading concern | Integration test, not data |
| Auth race conditions | Concurrency, not content | `test_admin.py` + load test |
| Judge LLM rate-limited | Operational | Circuit breaker pattern in `scaling/active_learning.py` |
| GPU eviction | Infra | Ray spot-tolerance + KubeRay restart |
| DB connection drop mid-transaction | Infra | SQLAlchemy session_scope + retry |
| Prompt injection in transcripts | Adversarial security concern | Out of scope for this iteration; address in dedicated security review |

### Skip for now — defer until they materialize

| Edge case | Why defer |
|---|---|
| **Multilingual transcripts** (Spanish, French, …) | Requires real-language fixtures + multilingual sentiment labels; meaningful only when a customer requests non-English support. ADR 0002 documents the trigger. |
| **PII redaction failures** | Would need a Presidio-style upstream pass to test against; that pass is itself a separate component. |
| **Speaker diarization errors** | Upstream-tooling concern; the analysis pipeline assumes clean speaker labels. |

---

## How the synthetic fixtures are organized

```
tests/fixtures/synthetic/
├── README.md
├── gen_synthetic.py              # programmatic generator from templates
├── title_variants/               # 5 meeting dirs covering the title cases
├── customer_unicode/             # 3 meetings · accented/punctuated names
├── net_new_product/              # 2 meetings · products outside keyword list
├── all_neutral/                  # 1 long meeting · zero sentiment variance
├── single_sentence/              # 1 meeting · degenerate trajectory input
├── multi_incident/               # 1 meeting · two incidents referenced
├── historical_incident/          # 1 meeting · past-tense outage
└── internal_mentions_customer/   # 1 meeting · classification ambiguity
```

Each fixture is a directory in the same shape as a real meeting record (one or more of `meeting-info.json`, `transcript.json`, `summary.json`, `speakers.json`). The generator (`gen_synthetic.py`) can produce additional fixtures from templates — useful when expanding coverage, or when a customer's title format prompts a new case.

---

## How they're wired in

1. **Pipeline tests** — `tests/test_edge_cases.py` loads each synthetic dir and asserts:
   - The categorizer produces a sensible label (or correctly falls into the catch-all)
   - Sentiment trajectories don't crash on degenerate input
   - The customer extractor handles unicode names
   - Net-new products land in the `General` product fallback, not silently disappear

2. **Validation audits** — `python validate.py --extra-dataset tests/fixtures/synthetic` runs the same 10 audits against the union of real + synthetic data. Useful as a CI step before shipping a categorization rule change.

3. **Manual review** — for each new synthetic fixture, the contributor adds an entry to the table above with an honest description of *what's being tested* and *what success looks like*.

---

## When to add a new synthetic case

Add one when:
- A real customer's data exposes a category miss → reproduce in a synthetic fixture before fixing the rule
- A new product launches → add a fixture before the rules update so the test fails first, then passes after
- An incident reveals an unhandled trajectory shape → fixture + trajectory-math fix together
- Validation flags a coverage hole that's repeatable in production but absent from organic traffic so far

Don't add one for:
- One-off curiosity ("what would happen if…?") — that's an exploration, not a fixture
- Things that are better tested as units
- Adversarial inputs without a corresponding security review process

The principle: **a synthetic fixture exists to keep a regression from sneaking back in.** If you can't write a clear "this should fail before the fix and pass after" check, the fixture probably shouldn't exist.
