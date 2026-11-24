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
