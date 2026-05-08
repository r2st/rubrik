# Incident runbooks

Concrete playbooks for the incident classes the cloud-agnostic research
calls out. One section per incident: what to check first, what to do to
mitigate the user-visible damage, what to follow up with after the page
clears.

For backup / PITR / cross-region failover see [`dr-runbook.md`](dr-runbook.md).
For the underlying SLO targets that gate alerting, see [`slos.md`](slos.md).

---

## 1. OLTP latency spike

**Trigger.** `APIp95LatencyBreach` Prometheus alert, p95 > 200 ms sustained for 5+ min.
**Likely cause.** Replica lag, lock contention, query-plan regression after a deploy, hot-key concentration, vacuum stalled.

### First checks
```bash
# 1. p95 by route — narrow to the bad endpoints
histogram_quantile(0.95,
  sum(rate(http_request_duration_seconds_bucket{job="transcript-intel-api"}[5m]))
  by (le, handler))

# 2. DB CPU + connection wait
SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
SELECT * FROM pg_stat_activity WHERE wait_event_type = 'Lock';

# 3. Slow query sample (last 5 min)
SELECT query, total_exec_time, calls, mean_exec_time
FROM pg_stat_statements
WHERE mean_exec_time > 100
ORDER BY total_exec_time DESC LIMIT 10;

# 4. Replica lag
SELECT now() - pg_last_xact_replay_timestamp() AS replica_lag;
```

### Immediate mitigation
- **Trip the adaptive throttle** by lowering `slo_p95_ms` runtime setting if the breach is deep — sheds traffic while you investigate.
- **Tighten rate limits** on heavy endpoints via `rate_limit.per_tenant` for the offending tenant, if isolated.
- **Pause heavy backfills** running against the same DB.
- **Route reads to a stale replica** if the SLO can tolerate it for affected endpoints.
- **Open the circuit breaker manually** on the slow downstream call (deploy a config change setting `failure_threshold=1`).

### Follow-up
- Add or remove indexes once the offending query shape is identified.
- If hot-key concentration: re-evaluate the partition / shard key per ADR 0014's partitioning guidance.
- File an ADR amendment if the cause is structural (e.g., a query pattern that should never have hit the OLTP).

---

## 2. Consumer lag surge

**Trigger.** `StreamLagSLOBreach` (>60s lag for 5+ min) or KEDA scale-up failing to clear backlog.
**Likely cause.** Partition skew (one partition is hot), broker health, slow consumer, retry-storm cascade, downstream sink backed up.

### First checks
```bash
# 1. Per-partition lag
kafka-consumer-groups.sh --bootstrap-server $BROKERS \
  --describe --group transcript-intel.consumers

# 2. Broker health
kafka-topics.sh --bootstrap-server $BROKERS --describe \
  --topic transcript-intel.events | grep -E 'Isr:|Replicas:'

# 3. Per-consumer throughput
kubectl logs -n prod deploy/transcript-intel-kafka-consumers --tail=200 \
  | grep -E 'msg/s|throughput'
```

### Immediate mitigation
- **Scale consumers manually** if KEDA's pollingInterval is too coarse:
  ```bash
  kubectl scale deployment transcript-intel-kafka-consumers -n prod --replicas=12
  ```
- **Pause optional sinks** (analytics/search loaders) so the priority sinks (cache invalidation, audit trail) catch up first.
- **Drain the DLQ separately** if it's growing — pump-and-replay rather than letting it pile up.
- **Increase partition count** only if you've confirmed the hot partition is genuinely a hot key (not a transient spike). Once added, partitions cannot be removed.

### Follow-up
- Revisit partition count, message size, batch size, idempotent retry policy.
- If partition skew is structural, change the message key — but plan a key rotation, not a flip (consumers will see duplicates during the transition).

---

## 3. Cache stampede

**Trigger.** Sudden Redis miss-rate spike, origin QPS cliff up, upstream API latency degrading.
**Likely cause.** Synchronized expiry of many keys (the herd), a deploy that flushed Redis, an invalidation event that cleared a hot prefix, single-flight not deployed in front of an expensive loader.

### First checks
```bash
# 1. Redis miss rate
redis-cli INFO stats | grep -E 'keyspace_(hits|misses)'

# 2. Origin QPS by endpoint
sum(rate(http_requests_total{job="transcript-intel-api"}[1m])) by (handler)

# 3. Are TTLs synchronized? Sample 100 keys.
redis-cli --scan --pattern 'cache:*' | head -100 \
  | xargs -I{} redis-cli TTL {} | sort | uniq -c
```

### Immediate mitigation
- **Enable single-flight** on the hot endpoint if not already (`api/caching.py::SingleFlight`). This is a code change, not config — keep the patch ready.
- **Add TTL jitter** if not already (`api/caching.py::ttl_with_jitter`). Look for any new cache writes that bypass the helper.
- **Serve stale-while-refresh** on non-critical endpoints by bumping `stale_while_revalidate` in `cached()` calls.
- **Pre-warm** the hot keys via a one-shot script if traffic permits.

### Follow-up
- Audit cache writes for places that bypass `ttl_with_jitter` and `SingleFlight`.
- Add an event-driven invalidation path so the next event-bus message can warm caches before clients miss.
- Reduce blast radius: if one invalidation cleared a whole prefix, narrow the invalidation to specific keys.

---

## 4. Pods pending / node exhaustion

**Trigger.** `kubectl get po -n prod | grep Pending` returns rows; alerts on Cluster Autoscaler add-failure.
**Likely cause.** Node pool quota, PDB blocking eviction, over-tight resource requests, taints/tolerations mismatch, spot-instance reclaim wave.

### First checks
```bash
kubectl get events -n prod --sort-by=.lastTimestamp | tail -30
kubectl describe pod <pending-pod> -n prod
kubectl top nodes
kubectl get pdb -n prod
```

### Immediate mitigation
- **Wait for Cluster Autoscaler** if Pending is recent (< 2 min); CA may already be adding nodes. Confirm via the CA log.
- **Relax over-tight requests** if the pod can run smaller — `kubectl edit deploy/...` to reduce `requests.cpu`.
- **Stop non-essential jobs** (snapshot CronJob, batch loaders) to free capacity for the API tier.
- **Bump the Karpenter NodePool limit** if the cluster has hit its declared cap (`deploy/k8s/cluster-autoscaler.yaml`).

### Follow-up
- Right-size requests against actual usage (`vpa-recommender` is helpful).
- Split workloads into separate node pools so a noisy batch can't starve the API.
- Review PDBs — `minAvailable: 1` on a 1-replica StatefulSet blocks all evictions.

---

## 5. Bad rollout

**Trigger.** `APIAvailabilityFastBurn` alert during a deploy, p95 regression on canary, error rate spike.
**Likely cause.** New version has a regression, a missing migration, a config change incompatible with old replicas, an external dep broke during the cut.

### First checks
```bash
# 1. Argo Rollout state
kubectl argo rollouts get rollout transcript-intel-api -n prod

# 2. Compare canary vs stable error rates
sum(rate(http_requests_total{
  service="transcript-intel-api-canary", status_class="5xx"
}[5m]))

sum(rate(http_requests_total{
  service="transcript-intel-api", status_class="5xx"
}[5m]))

# 3. Trace comparison — same request shape, before vs after the cut
# (use OTel collector + Tempo / Jaeger UI)
```

### Immediate mitigation
- **Auto-rollback should already have fired** via the AnalysisTemplates in `deploy/k8s/argo-rollout.yaml`. If it hasn't:
  ```bash
  kubectl argo rollouts abort transcript-intel-api -n prod
  kubectl argo rollouts undo transcript-intel-api -n prod
  ```
- **Freeze rollout** if it's still in progress:
  ```bash
  kubectl argo rollouts pause transcript-intel-api -n prod
  ```
- **For non-canary deploys** (Deployment, not Rollout), manually `kubectl rollout undo`.

### Follow-up
- Add the missing smoke check that should have caught this in CI.
- Add the missing migration guard (e.g., version compatibility check at startup).
- Tighten the AnalysisTemplate failure threshold if the regression slipped past — error rate gate at 0.995 is the default, lower it for sensitive services.

---

## 6. Outbox relayer falling behind

**Trigger.** `outbox_events.processed_at IS NULL` count growing; `OutboxLagSLOBreach` (custom alert).
**Likely cause.** Publisher (Kafka) unreachable, publisher rate-limited, relayer pod evicted, attempt-cap reached on poison messages.

### First checks
```sql
-- Unprocessed count
SELECT COUNT(*) FROM outbox_events WHERE processed_at IS NULL;

-- Stuck rows (high attempts)
SELECT id, aggregate_type, event_type, delivery_attempts, created_at
FROM outbox_events
WHERE processed_at IS NULL AND delivery_attempts >= 3
ORDER BY created_at LIMIT 20;
```

### Immediate mitigation
- **Confirm publisher health** — Kafka broker reachable, ACLs intact, topic exists.
- **Restart the relayer** if it's stuck on a transient error.
- **Manually retry stuck rows** by resetting `delivery_attempts` for non-poison rows.
- **Move poison rows to DLQ** by inserting into the DLQ topic and deleting from outbox.

### Follow-up
- Verify the publisher's idempotency contract — duplicates after restarts are expected; consumers must dedupe.
- If the same `aggregate_id + sequence` combo keeps poisoning, the producer is emitting bad data — fix at the source.
- Add a poison-row alert: any row with `delivery_attempts >= max_attempts - 1`.

---

## How these connect to alerts

| Runbook section | Triggering alert(s) | Severity |
|---|---|---|
| OLTP latency spike | `APIp95LatencyBreach` | Sev-3 |
| Consumer lag surge | `StreamLagSLOBreach` | Sev-3 |
| Cache stampede | (no dedicated alert; observed via origin-QPS spike + Redis miss-rate) | Sev-3 |
| Pods pending | k8s `KubePodNotReady` | Sev-3 |
| Bad rollout | `APIAvailabilityFastBurn` during deploy window | Sev-2 |
| Outbox relayer lag | `OutboxLagSLOBreach` (TODO: add to Prometheus rules) | Sev-3 |

Every runbook should be exercised at least once a quarter — pick a category, run the failure injection (`chaos-mesh` or equivalent), confirm the alert fires and the runbook's mitigation works.
