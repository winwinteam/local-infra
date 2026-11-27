#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "opentelemetry-api>=1.27",
#     "opentelemetry-sdk>=1.27",
#     "opentelemetry-exporter-otlp-proto-http>=1.27",
# ]
# ///
"""End-to-end OTel + Kafka round-trip test against a standalone Aspire Dashboard.

1. WRITE: Emits a dummy trace via OTLP/HTTP protobuf that mimics a Kafka flow:
     http-request (SERVER)
       └─ business-events publish (PRODUCER)  -> injects W3C traceparent into record headers
            └─ business-events process (CONSUMER) <- extracts traceparent from record headers
   Kafka record identity (topic/partition/offset/key/message-id) is attached as
   OTel messaging semantic-convention attributes plus a `kafka.message` span event,
   and correlated structured logs are emitted inside the producer/consumer spans.
   No broker is needed: the Kafka record is an in-memory dict carrying real headers.

2. READ: Queries the Aspire Dashboard telemetry HTTP API (Aspire >= 13.2):
     GET {UI}/api/telemetry/traces/{traceId}
     GET {UI}/api/telemetry/logs?resource=<service>
   and validates spans, parent/child links, Kafka attributes, span events and logs.

Usage:
    uv run --script scripts/otel_kafka_roundtrip.py \
        --otlp http://127.0.0.1:4318 --ui http://127.0.0.1:18888 [--api-key KEY]
Exit code 0 = pass, 1 = validation failure, 2 = connectivity/setup error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from opentelemetry import propagate
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind

TOPIC = "business-events"
PARTITION = 0
OFFSET = 4242


# --------------------------------------------------------------------------- write


def emit(otlp: str, service: str, run_id: str, headers: dict[str, str]) -> dict[str, Any]:
    resource = Resource.create(
        {
            "service.name": service,
            "service.instance.id": "smoke-test",
            "deployment.environment": "test",
        }
    )
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp}/v1/traces", headers=headers))
    )
    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{otlp}/v1/logs", headers=headers))
    )
    set_logger_provider(lp)

    log = logging.getLogger(f"aspire.roundtrip.{run_id}")
    log.setLevel(logging.INFO)
    log.propagate = False
    log.addHandler(LoggingHandler(level=logging.INFO, logger_provider=lp))

    tracer = tp.get_tracer("aspire-roundtrip")
    message_id = f"msg-{run_id}"
    key = f"supplier-{run_id[:8]}"
    payload = json.dumps({"event_id": message_id, "supplier": key, "amount": 123.45})
    kafka_attrs = {
        "messaging.system": "kafka",
        "messaging.destination.name": TOPIC,
        "messaging.destination.partition.id": str(PARTITION),
        "messaging.kafka.message.offset": OFFSET,
        "messaging.kafka.message.key": key,
        "messaging.message.id": message_id,
    }

    with tracer.start_as_current_span("POST /api/runs/stages/emit", kind=SpanKind.SERVER) as root:
        root.set_attribute("test.run_id", run_id)
        trace_id = format(root.get_span_context().trace_id, "032x")

        # ---- Kafka producer: inject traceparent into record headers
        record: dict[str, Any] = {"key": key, "value": payload, "headers": {}}
        with tracer.start_as_current_span(f"{TOPIC} publish", kind=SpanKind.PRODUCER) as prod:
            prod.set_attributes({**kafka_attrs, "messaging.operation.type": "publish"})
            propagate.inject(record["headers"])
            prod.add_event(
                "kafka.message",
                {**kafka_attrs, "kafka.payload.preview": payload[:512]},
            )
            log.info(
                "Produced Kafka record %s to %s[%d]@%d",
                message_id,
                TOPIC,
                PARTITION,
                OFFSET,
                extra={**kafka_attrs, "test.run_id": run_id},
            )
            producer_span_id = format(prod.get_span_context().span_id, "016x")

    # ---- Kafka consumer (separate "process"): extract traceparent from headers
    ctx = propagate.extract(record["headers"])
    with tracer.start_as_current_span(
        f"{TOPIC} process", context=ctx, kind=SpanKind.CONSUMER
    ) as cons:
        cons.set_attributes({**kafka_attrs, "messaging.operation.type": "process"})
        cons.add_event("kafka.message", {**kafka_attrs, "kafka.payload.preview": payload[:512]})
        log.info(
            "Consumed Kafka record %s from %s[%d]@%d",
            message_id,
            TOPIC,
            PARTITION,
            OFFSET,
            extra={**kafka_attrs, "test.run_id": run_id},
        )

    tp.force_flush(10_000)
    lp.force_flush(10_000)
    tp.shutdown()
    lp.shutdown()

    return {
        "trace_id": trace_id,
        "producer_span_id": producer_span_id,
        "traceparent": record["headers"].get("traceparent", ""),
        "message_id": message_id,
        "key": key,
    }


# --------------------------------------------------------------------------- read


def http_get(url: str, api_key: str | None) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        req.add_header("x-api-key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def attr_map(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Flatten OTLP JSON attributes [{key, value:{stringValue|intValue|...}}]."""
    out: dict[str, Any] = {}
    for a in attrs or []:
        v = a.get("value", {})
        out[a["key"]] = next(iter(v.values()), None) if isinstance(v, dict) else v
    return out


def walk_spans(doc: Any) -> list[dict[str, Any]]:
    """Collect spans from OTLP JSON ({resourceSpans:[{scopeSpans:[{spans:[...]}]}]})."""
    spans: list[dict[str, Any]] = []
    if isinstance(doc, dict):
        if "spans" in doc and isinstance(doc["spans"], list):
            spans.extend(s for s in doc["spans"] if isinstance(s, dict) and "spanId" in s)
        for v in doc.values():
            if isinstance(v, (dict, list)):
                spans.extend(walk_spans(v))
    elif isinstance(doc, list):
        for v in doc:
            spans.extend(walk_spans(v))
    return spans


def walk_logs(doc: Any) -> list[dict[str, Any]]:
    """Collect log records from OTLP JSON ({resourceLogs:[{scopeLogs:[{logRecords:[...]}]}]})."""
    recs: list[dict[str, Any]] = []
    if isinstance(doc, dict):
        if isinstance(doc.get("logRecords"), list):
            recs.extend(doc["logRecords"])
        for k, v in doc.items():
            if k != "logRecords" and isinstance(v, (dict, list)):
                recs.extend(walk_logs(v))
    elif isinstance(doc, list):
        for v in doc:
            recs.extend(walk_logs(v))
    return recs


def norm_id(v: str | None) -> str:
    return (v or "").lower()


class Checker:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
        if not ok:
            self.failures += 1


def poll(fn, timeout: float, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    return last


def validate(
    ui: str, api_key: str | None, service: str, sent: dict[str, Any], timeout: float
) -> int:
    c = Checker()
    tid = sent["trace_id"]

    # ---- API availability
    code, _ = http_get(f"{ui}/api/telemetry/resources", api_key)
    if code != 200:
        print(
            f"ERROR: GET {ui}/api/telemetry/resources -> HTTP {code}. The telemetry API needs "
            "aspire-dashboard >= 13.2 with Dashboard__Api__Enabled=true "
            "(and Dashboard__Api__AuthMode=Unsecured or a matching --api-key).",
            file=sys.stderr,
        )
        return 2

    # ---- Trace read-back
    def fetch_spans():
        code, doc = http_get(f"{ui}/api/telemetry/traces/{tid}", api_key)
        spans = walk_spans(doc) if code == 200 else []
        return spans if len(spans) >= 3 else None

    spans = poll(fetch_spans, timeout) or []
    print(f"\n--- Trace {tid} ({len(spans)} spans) ---")
    by_name = {s.get("name"): s for s in spans}
    c.check(len(spans) == 3, "trace contains 3 spans", f"got {sorted(by_name)}")
    c.check(all(norm_id(s.get("traceId")) == tid for s in spans), "all spans share trace_id")

    prod = by_name.get(f"{TOPIC} publish")
    cons = by_name.get(f"{TOPIC} process")
    c.check(prod is not None, "producer span present")
    c.check(cons is not None, "consumer span present")

    if prod and cons:
        c.check(
            norm_id(cons.get("parentSpanId"))
            == norm_id(prod.get("spanId"))
            == sent["producer_span_id"],
            "consumer parent == producer (traceparent propagated via Kafka headers)",
            f"traceparent={sent['traceparent']}",
        )
        for label, span in (("producer", prod), ("consumer", cons)):
            a = attr_map(span.get("attributes"))
            c.check(a.get("messaging.system") == "kafka", f"{label}: messaging.system=kafka")
            c.check(a.get("messaging.destination.name") == TOPIC, f"{label}: topic={TOPIC}")
            c.check(
                str(a.get("messaging.kafka.message.offset")) == str(OFFSET),
                f"{label}: offset={OFFSET}",
                f"got {a.get('messaging.kafka.message.offset')!r}",
            )
            c.check(
                a.get("messaging.message.id") == sent["message_id"],
                f"{label}: message.id={sent['message_id']}",
            )
            evs = [e for e in span.get("events", []) if e.get("name") == "kafka.message"]
            preview = attr_map(evs[0].get("attributes")).get("kafka.payload.preview") if evs else ""
            c.check(
                bool(evs) and sent["message_id"] in (preview or ""),
                f"{label}: kafka.message span event with payload preview",
            )

    # ---- Log read-back (correlated to trace)
    def fetch_logs():
        q = urllib.parse.urlencode({"traceId": tid})
        code, doc = http_get(f"{ui}/api/telemetry/logs?{q}", api_key)
        recs = (
            [r for r in walk_logs(doc) if norm_id(r.get("traceId")) == tid] if code == 200 else []
        )
        return recs if len(recs) >= 2 else None

    logs = poll(fetch_logs, timeout) or []
    print(f"\n--- Logs correlated to trace ({len(logs)}) ---")
    c.check(len(logs) >= 2, "2 log records carry the trace_id")
    span_ids = {norm_id(s.get("spanId")) for s in spans}
    c.check(
        bool(logs) and all(norm_id(r.get("spanId")) in span_ids for r in logs),
        "log span_ids resolve to spans in the trace",
    )
    c.check(
        any(
            attr_map(r.get("attributes")).get("messaging.message.id") == sent["message_id"]
            for r in logs
        ),
        "log carries messaging.message.id",
    )

    print(f"\nView in UI: {ui}/traces/detail/{tid}")
    return 0 if c.failures == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--otlp", default="http://127.0.0.1:4318", help="OTLP/HTTP base URL")
    p.add_argument("--ui", default="http://127.0.0.1:18888", help="Aspire Dashboard UI base URL")
    p.add_argument(
        "--api-key", default=None, help="Telemetry API key (Dashboard__Api__AuthMode=ApiKey)"
    )
    p.add_argument("--otlp-api-key", default=None, help="OTLP ingest key (x-otlp-api-key)")
    p.add_argument("--timeout", type=float, default=20.0, help="Read-back timeout (s)")
    args = p.parse_args()

    run_id = uuid.uuid4().hex
    service = "aspire-roundtrip-test"
    otlp_headers = {"x-otlp-api-key": args.otlp_api_key} if args.otlp_api_key else {}

    print(f"=== Emitting dummy OTel + Kafka trace (run {run_id[:12]}) -> {args.otlp} ===")
    try:
        sent = emit(args.otlp.rstrip("/"), service, run_id, otlp_headers)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: emit failed: {e}", file=sys.stderr)
        return 2
    print(f"  trace_id={sent['trace_id']}  message_id={sent['message_id']}")
    print(f"  kafka headers: traceparent={sent['traceparent']}")

    print(f"\n=== Reading back from Aspire telemetry API ({args.ui}) ===")
    rc = validate(args.ui.rstrip("/"), args.api_key, service, sent, args.timeout)
    print("\nRESULT:", {0: "PASS", 1: "FAIL", 2: "ERROR"}[rc])
    return rc


if __name__ == "__main__":
    sys.exit(main())
