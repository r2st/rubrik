# SLO catalogue

ADR 0014 §"SLOs" adopts the cloud-agnostic research's recommended SLO set as
the operational target. SRE practice dictates these become **the only thing
the on-call rotation alerts on** — not raw threshold breaches.

## Defaults

| SLI | SLO | Notes |
|---|---|---|
| Public API availability | **99.95% monthly** | 21.6 minutes of allowed downtime/month. Excludes scheduled maintenance windows announced in advance. |
| Cached read latency (p95) | **≤ 50 ms** | `summary`, `customer-health`, `clusters` after warm cache. |
| Cached read latency (p99) | **≤ 120 ms** | |
| Uncached transactional read latency (p95) | **≤ 150 ms** | First request after a cache miss. |
| Uncached transactional read latency (p99) | **≤ 400 ms** | |
| Transactional write latency (p95) | **≤ 250 ms** | Admin settings update, password rotation, snapshot rebuild enqueue. |
| Transactional write latency (p99) | **≤ 600 ms** | |
| Stream end-to-end freshness (lag) | **≤ 60 s normally**, **≤ 15 min after a 10× burst** | Outbox row → consumer ack on the event backbone. |
| Backup recoverability | Successful **monthly** restore drill, **quarterly** region failover drill | Failure of either drill is a Sev-2 incident. |

## How they're enforced in code + ops

| SLO | Enforcement point |
|---|---|
| API availability | `BackpressureMiddleware` sheds 503s before request pile-up cascades. `circuit_breaker.py` opens on sustained downstream failures. |
| p95 read latency | HPA target in `deploy/k8s/hpa.yaml` is `http_request_duration_p95: 200m` (200 ms). Slot is tighter than the SLO so HPA acts before the SLO burns. |
| p99 latency | OTel collector tail-sampling captures every slow tail (`deploy/k8s/otel-collector.yaml`) so root-causing is possible. |
| Stream freshness | KEDA scales consumers on lag (`deploy/k8s/keda-scaledobjects.yaml`); lag thresholds aligned with the SLO. |
| Backup drills | Documented in `deploy/dr-runbook.md`; tracked as a recurring calendar event. |

## Burn-rate alerting

We alert on **error-budget burn rate**, not raw threshold breach. The two
windows that matter:

| Window | Burn rate | Meaning |
|---|---|---|
| 1 hour | 14.4× | 2% of the monthly budget consumed in 1h → page Sev-2 |
| 6 hours | 6× | 5% of the monthly budget consumed in 6h → page Sev-3 |

Prometheus rules live in the platform's monitoring repo and reference the
HTTP histograms emitted by `prometheus-fastapi-instrumentator`.

## Revisiting

These SLOs are **defaults**. Once the system is in production the targets
should be re-tuned to user-perceived experience — measured, not guessed.
The right time to tighten p95 to 30 ms is when the latency budget is being
consistently underspent at 50 ms; the right time to relax stream freshness
from 60 s to 5 min is when no consumer actually depends on sub-minute
freshness.
