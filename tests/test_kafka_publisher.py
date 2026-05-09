"""Surface tests for the optional Kafka publisher.

End-to-end Kafka tests need a real broker (testcontainers, embedded
Kafka). Those run in the CI integration job — not here. What we DO
test in unit-mode:

  - The constructor surfaces a useful error when ``confluent-kafka``
    isn't installed (the optional-dep guard).
  - ``_build_traceparent`` shape is W3C-conformant.
"""
from __future__ import annotations

import re

import pytest


def test_traceparent_is_well_formed():
    from api.outbox_kafka import _build_traceparent
    tp = _build_traceparent("a" * 32).decode("ascii")
    assert re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", tp,
    ), f"bad traceparent: {tp}"


def test_kafka_publisher_raises_clear_error_when_dep_missing(monkeypatch):
    """Without confluent-kafka installed, constructing the publisher must
    surface a helpful message rather than a generic ImportError."""
    import sys
    monkeypatch.setitem(sys.modules, "confluent_kafka", None)
    from api.outbox_kafka import KafkaPublisher
    with pytest.raises(RuntimeError, match="confluent-kafka"):
        KafkaPublisher(bootstrap_servers="localhost:9092",
                       topic="transcript-intel.events")
