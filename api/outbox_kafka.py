"""Kafka publisher for the outbox relayer (ADR 0014 §"Stream").

Bridges ``api/outbox.py``'s synchronous ``Publisher`` protocol to Kafka.
Used by the relayer worker (``deploy/k8s/worker.yaml``) when
``[runtime].redis_url`` is set + ``auth.kafka_bootstrap`` is configured.

Production wiring:

  publisher = KafkaPublisher(
      bootstrap_servers=settings.kafka_bootstrap,
      topic="transcript-intel.events",
  )
  dlq = KafkaPublisher(
      bootstrap_servers=settings.kafka_bootstrap,
      topic="transcript-intel.events.dlq",
  )
  relayer = Relayer(publisher=DLQPublisher(publisher, dlq))

Why ``confluent-kafka`` over ``aiokafka``: the outbox relayer is a
short-loop synchronous worker (``drain_once`` returns int → caller
sleeps), so adding asyncio + a separate event loop is overhead with
no upside. ``confluent-kafka`` ships a synchronous ``Producer`` that's
also faster.

The dependency is **optional** — ``import confluent_kafka`` is lazy so
deployments without Kafka don't carry the library. The
``KafkaPublisher`` constructor raises immediately if the import fails;
the ``InMemoryPublisher`` is the fallback.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from src.logging_config import get_logger

from .outbox import OutboxRecord

log = get_logger(__name__)


class KafkaPublisher:
    """Synchronous Kafka producer that satisfies ``outbox.Publisher``.

    One producer instance can be shared across publishes. ``publish()``
    blocks on broker acknowledgement (``acks=all`` for durability) so
    relayer pacing matches broker throughput.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        topic: str,
        client_id: str = "transcript-intel-relayer",
        config_overrides: Optional[dict] = None,
    ) -> None:
        try:
            from confluent_kafka import Producer  # noqa: PLC0415
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "KafkaPublisher requires the 'confluent-kafka' package. "
                "Install it (or use InMemoryPublisher for dev)."
            ) from e

        # Production-friendly defaults. Operators can override via
        # ``config_overrides`` for brokers that need SASL/SSL/etc.
        config: dict[str, Any] = {
            "bootstrap.servers": bootstrap_servers,
            "client.id": client_id,
            "acks": "all",                          # durability
            "enable.idempotence": True,             # exactly-once semantics
            "compression.type": "lz4",
            "linger.ms": 5,                         # batch a little
            "max.in.flight.requests.per.connection": 5,
            "delivery.timeout.ms": 30_000,
        }
        if config_overrides:
            config.update(config_overrides)

        self._topic = topic
        self._producer = Producer(config)
        self._delivery_failure: Optional[Exception] = None

    def publish(self, event: OutboxRecord) -> None:
        """Send + flush, raising on delivery failure.

        Synchronous: blocks until the broker acks (or fails). The relayer's
        per-row try/except handles failures by incrementing
        ``delivery_attempts``; this method only needs to surface them.
        """
        # Headers carry the trace ID so consumers can extend the span.
        headers = []
        if event.trace_id:
            headers.append(("traceparent", _build_traceparent(event.trace_id)))
        headers.append(("event_type", event.event_type.encode("utf-8")))
        headers.append(("aggregate_type", event.aggregate_type.encode("utf-8")))

        payload = json.dumps({
            "id": event.id,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "event_type": event.event_type,
            "sequence": event.sequence,
            "payload": event.payload,
            "trace_id": event.trace_id,
        }).encode("utf-8")

        # Key the message on aggregate_id for partition affinity — all
        # events for one aggregate land on the same partition, preserving
        # order on the consumer side.
        self._delivery_failure = None
        self._producer.produce(
            self._topic,
            key=event.aggregate_id.encode("utf-8"),
            value=payload,
            headers=headers,
            on_delivery=self._on_delivery,
        )
        # Flush blocks until acked (or until the delivery_timeout_ms above
        # fires). Setting a tight per-message flush turns the producer into
        # a synchronous publisher with the relayer's pacing.
        self._producer.flush(10.0)
        if self._delivery_failure is not None:
            raise self._delivery_failure

    def _on_delivery(self, err, _msg) -> None:
        """Delivery callback — capture errors so ``publish()`` can re-raise."""
        if err is not None:
            self._delivery_failure = RuntimeError(
                f"Kafka delivery failed: {err}"
            )

    def close(self) -> None:  # pragma: no cover — used in graceful shutdown
        try:
            self._producer.flush(30.0)
        except Exception:  # noqa: BLE001
            log.exception("Kafka producer flush during close failed")


def _build_traceparent(trace_id_hex: str) -> bytes:
    """W3C ``traceparent`` header value with a synthetic span ID.

    Format: ``00-<trace_id:32hex>-<span_id:16hex>-01``. Consumers seed
    their span context from this so the trace continues end-to-end. The
    span ID is randomised — the relayer's publish is itself a span, but
    we don't carry that forward (the publish span is a leaf).
    """
    import secrets
    span_id = secrets.token_hex(8)
    return f"00-{trace_id_hex}-{span_id}-01".encode("ascii")
