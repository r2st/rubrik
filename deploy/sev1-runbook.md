# Sev-1 Incident Response Runbook

A Sev-1 is **customer-visible API down or > 5% error budget burn over 1 h**.
This runbook is the on-caller's checklist. It assumes you've been paged
and you're at a laptop within 5 minutes.

## 0. Acknowledge (≤ 2 min)

1. Acknowledge the page in PagerDuty.
2. Open `#inc-live` in Slack and post the standard opener:
   ```
   Sev1 · acknowledged · oncall: @<you>
   triage starting · status updates every 10 min
   ```
3. Spawn an incident channel `#inc-YYYYMMDD-<short-handle>` and pin the
   Grafana SLO dashboard + this runbook there.

## 1. Confirm scope (≤ 5 min)

- Grafana → **Transcript Intelligence · SLO overview** dashboard:
  - p95 latency
  - 5xx rate
  - readiness gauge per replica
  - adaptive throttle shed-rate
  - circuit-breaker state per dependency
- Synthetic probe: `make smoke-test BASE_URL=https://api.example.com`.
- If the smoke test passes but the dashboard is red → noisy alert,
  re-tune SLO and downgrade. Otherwise continue.

## 2. Identify the failure mode (≤ 10 min)

Look at the dashboard panels in this order — first match is the lead:

| Symptom | Probable cause | Jump to |
|---|---|---|
| `pg_up == 0` or DB latency cliff | Postgres outage | §3.1 |
| `redis_up == 0` | Redis outage (rate limit + idempotency degrade silently to in-process) | §3.2 |
| Adaptive throttle shedding > 10 % | Saturation — usually downstream slowdown amplified | §3.3 |
| Circuit breaker stuck open on `tier2_llm` | LLM provider outage | §3.4 |
| Outbox `outbox_unprocessed` rising > 10k | Relayer / Kafka lag | §3.5 |
| 5xx rate without dependency signal | Recent deploy | §3.6 |
| All of the above flapping | Likely Kubernetes-level (node, network) | §3.7 |

## 3. Mitigations

### 3.1 Postgres outage
- Failover: `kubectl -n db patch postgrescluster main --type merge -p '{"spec":{"failover":true}}'` — confirms replica promotion.
- If failover doesn't work, restore from PITR (see `deploy/dr-runbook.md` §3).
- **While DB is sick:** flip `transcripts.repository=local` in the admin
  panel — the snapshot-loaded `LocalDirectoryRepository` keeps reads
  serving (writes still error, that's the price).

### 3.2 Redis outage
- Confirm: `kubectl -n redis exec sts/redis 0 -- redis-cli ping`.
- Failover the StatefulSet, or temporarily blank `distribution.redis_url`
  in the admin panel — every Redis-backed subsystem (rate limit,
  idempotency, LLM cache, JWKS cache) silently degrades to in-process.
  No customer impact beyond slightly higher latency on cold paths.

### 3.3 Saturation / shed-rate spike
- Look for a hot tenant: Grafana → **Per-tenant request rate**.
- If a single tenant > 30 % of total traffic, lower their cap in
  admin panel (`rate_limit.per_tenant`) — settings push propagates
  in < 100 ms via `LISTEN/NOTIFY`. Document the change in `#inc-live`.
- If broad: scale up. HPA + KEDA should already be reacting; if they
  aren't, check `kubectl describe hpa transcript-intel-api`.

### 3.4 LLM Tier-2 outage
- Self-healing: the circuit breaker opens automatically and the cascade
  falls through to Tier-1 Gemma. Customer impact is **degraded answer
  quality**, not error.
- If unacceptable, the operator can flip `llm.tier2_enabled=false` in
  the admin panel and post in `#inc-live`.

### 3.5 Outbox relayer lag
- `kubectl logs -n app deploy/outbox-relayer --tail=200`.
- Common causes: Kafka broker down, DLQ topic full, schema-registry
  rejection. Each mitigated separately — see `deploy/incident-runbooks.md`
  §"Outbox lag".

### 3.6 Recent deploy → roll back
- Argo Rollouts is configured to roll back on SLO breach automatically.
  If it hasn't: `kubectl argo rollouts undo transcript-intel-api -n app`.
- Capture the bad image SHA in the incident channel before it's gone.

### 3.7 Kubernetes / network
- `kubectl get nodes` — any `NotReady`?
- `kubectl get pods -n app` — pending replicas?
- If multi-AZ outage: trigger cross-region failover per
  `deploy/dr-runbook.md` §5.

## 4. Customer comms (≤ 15 min after confirm)

If user-facing impact is confirmed:
- Post on `status.example.com` (Statuspage). Template lives in
  `deploy/statuspage-templates.md`.
- Email any tenant whose traffic dropped to zero — pull from the
  per-tenant Grafana panel.
- DO NOT name customers in public Statuspage updates.

## 5. Resolution

- Smoke test green, SLO dashboard green for 15 min sustained → resolve
  the page.
- Post in `#inc-live`:
  ```
  Sev1 · resolved · duration: <X> min
  preliminary cause: <one sentence>
  postmortem doc: <link>
  ```

## 6. Postmortem (within 5 business days)

- Use the template in `docs/postmortem-template.md`.
- **Blameless.** Focus on the system, not the operator.
- Action items get JIRA tickets with severity-weighted SLAs:
  - "would have prevented" → 30 d
  - "would have shortened" → 60 d
  - "monitoring gap" → 14 d (cheapest, highest leverage)

## Appendix · contact escalation

| Tier | Channel | Reach |
|---|---|---|
| 0 | PagerDuty primary on-call | < 5 min |
| 1 | PagerDuty secondary on-call | < 15 min |
| 2 | Engineering manager (text) | < 30 min |
| 3 | VP Engineering (call) | < 1 h |

If the on-call rotation is broken (page didn't fire, or primary
unreachable for 10 min), call the EM directly.
