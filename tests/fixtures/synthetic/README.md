# Synthetic edge-case fixtures

Hand-crafted meeting directories that exercise corners the client's sample doesn't reach. See [`docs/edge-cases.md`](../../../docs/edge-cases.md) for the full rationale.

## Layout

Each subdirectory follows the same JSON shape as the real dataset:

```
<edge_case_category>/
└── <slug>/
    ├── meeting-info.json       # title, dates, organizer
    ├── transcript.json         # per-sentence text + sentiment + speaker
    ├── speakers.json           # speaker segments with timing
    ├── speaker-meta.json       # speaker_id → name map
    ├── summary.json            # paragraph summary + action items + sentiment
    └── events.json             # join/leave events
```

The `data_loader` will pick these up identically to the real dataset directories — they're indistinguishable from the pipeline's perspective.

## How to regenerate / add cases

```bash
# Regenerate all default cases (idempotent)
python tests/fixtures/synthetic/gen_synthetic.py

# Regenerate just one
python tests/fixtures/synthetic/gen_synthetic.py --case multi_incident
```

To add a new edge case:
1. Write a `case_<name>(out)` function in `gen_synthetic.py`
2. Register it in `DEFAULT_CASES`
3. Add an entry to the table in `docs/edge-cases.md`
4. Add an assertion to `tests/test_edge_cases.py`

The principle is in `docs/edge-cases.md`: a fixture only earns its place if you can write a clear "this should fail before the fix and pass after."

## How they're used in CI

- `tests/test_edge_cases.py` loads each fixture and asserts the pipeline produces the expected categorization, doesn't crash on degenerate input, and handles unicode names.
- `python validate.py --extra-dataset tests/fixtures/synthetic` runs the same 10 semantic audits across the union of real + synthetic data, catching coverage regressions before they ship.
