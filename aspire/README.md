# Aspire Dashboard Local Observability Stack

Lightweight (~40–100 MiB RAM), 100% offline/local OpenTelemetry collector and UI powered by the standalone [.NET Aspire Dashboard](https://learn.microsoft.com/en-us/dotnet/aspire/fundamentals/dashboard/standalone) (`mcr.microsoft.com/dotnet/aspire-dashboard:9.0`), tuned for **Podman** (`podman compose`) with automatic fallback to `podman-compose` and `docker compose`.

## Service URLs & Ports

| Component | Host Port | Description |
|---|---|---|
| **Aspire Web UI** | `18888` | Distributed traces, structured logs, and metrics UI ([http://127.0.0.1:18888](http://127.0.0.1:18888)) |
| **OTLP HTTP** | `4318` | Ingests OTLP/HTTP Protobuf (`POST /v1/traces`, `/v1/logs`, `/v1/metrics`) |
| **OTLP gRPC** | `4317` | Ingests OTLP/gRPC telemetry |

> **Port Sharing with HyperDX:** Aspire and HyperDX intentionally bind the same OTLP ports (`4317` and `4318`) so all client applications use the exact same `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`. Run **either** `aspire` **or** `hyperdx` at a time — never both simultaneously.

---

## Quick Start (Podman / Docker)

```bash
cd aspire
cp .env.example .env
bash scripts/run_aspire.sh
```

`run_aspire.sh` automatically selects `podman compose` → `podman-compose` → `docker compose`, applies `.compose.sha256` change detection, waits for HTTP 200 on the Web UI (`:18888`) and OTLP HTTP receiver (`:4318`), and prints client environment variables.

### Running Smoke Tests

Verify container status, Web UI availability (`HTTP 200`), and OTLP HTTP `/v1/traces`, `/v1/logs`, and `/v1/metrics` ingestion (`HTTP 200`):

```bash
bash scripts/test_aspire.sh
```

Step 6 runs [`scripts/otel_kafka_roundtrip.py`](scripts/otel_kafka_roundtrip.py) with `uv run --script`. Its dependencies are declared inline (PEP 723), so no venv setup is needed. The script:
1. **Writes** a dummy trace over OTLP/HTTP: `HTTP → Kafka producer → Kafka consumer`. The W3C `traceparent` travels in in-memory Kafka record headers (no broker needed). Each span carries Kafka attributes (`messaging.destination.name`, `messaging.kafka.message.offset`, `messaging.message.id`, …) and a `kafka.message` span event with a payload preview. Two structured logs linked to the trace are emitted too.
2. **Reads back** from the Aspire telemetry HTTP API (`GET :18888/api/telemetry/traces/{traceId}` and `/api/telemetry/logs`). It checks the span parent/child links, the Kafka attributes, the span events, and the log ↔ trace correlation.

> **Requires `aspire-dashboard` ≥ 13.2** (`ASPIRE_IMAGE_TAG` in `.env`) with `Dashboard__Api__Enabled=true`. Version 9.x has no telemetry read API.

Run it standalone, for example against Aspire from inside the `krunvm` VM:

```bash
uv run --script scripts/otel_kafka_roundtrip.py --otlp http://127.0.0.1:4318 --ui http://127.0.0.1:18888
```

### Memory Tuning

Telemetry is kept **in memory only**, and the oldest data is evicted first. The caps live in `.env` and are sized for `ASPIRE_MEM_LIMIT=256m`:

| `.env` variable | Default | Dashboard setting |
|---|---|---|
| `ASPIRE_MAX_TRACE_COUNT` | `2000` | `Dashboard__TelemetryLimits__MaxTraceCount` |
| `ASPIRE_MAX_LOG_COUNT` | `5000` | `Dashboard__TelemetryLimits__MaxLogCount` |
| `ASPIRE_MAX_METRICS_COUNT` | `20000` | `Dashboard__TelemetryLimits__MaxMetricsCount` |
| `ASPIRE_MAX_ATTRIBUTE_LENGTH` | `1024` | `Dashboard__TelemetryLimits__MaxAttributeLength` (caps payload previews) |

### Verifying OTLP Reachability (e.g., from a `krunvm` VM to Host PC)

Aspire Dashboard 9.0 accepts OTLP/HTTP **Protobuf** (`application/x-protobuf`, which is the default for Python `opentelemetry-exporter-otlp-proto-http` and the Java `opentelemetry-javaagent`):

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST \
  -H "Content-Type: application/x-protobuf" \
  --data-binary "" \
  http://127.0.0.1:4318/v1/traces
# Expects: 200
```

### Stopping or Recreating

```bash
# Stop and remove container
podman compose down        # or: docker compose down

# View live logs
podman compose logs -f     # or: docker compose logs -f
```

---

## Switching Between Aspire and HyperDX

| Stack | Typical RAM | Persistence | Best For |
|---|---|---|---|
| **Aspire Dashboard** (`aspire/`) | ~40–100 MiB (capped at `256m`) | In-memory (ephemeral) | Fast daily local dev, laptop-friendly trace waterfalls |
| **HyperDX + ClickHouse** (`hyperdx/`) | ~2–3.5 GiB | Persistent volumes | Long-term search, SQL analytics, Grafana dashboards |

Because both stacks share ports `4317` and `4318`, stop one before starting the other:

```bash
# Switch from HyperDX -> Aspire
(cd ../hyperdx && docker compose down)
bash scripts/run_aspire.sh

# Switch from Aspire -> HyperDX
podman compose down
(cd ../hyperdx && bash scripts/run_hyperdx.sh)
```

---

## Client Integration Guide

Point any OpenTelemetry SDK or zero-code agent at the local OTLP HTTP endpoint:

```env
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=
```

No client code or endpoint URL changes are needed when switching between Aspire Dashboard and HyperDX.
