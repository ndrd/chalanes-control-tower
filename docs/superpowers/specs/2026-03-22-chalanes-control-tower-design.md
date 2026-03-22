# Chalanes Control Tower — Design Specification

**Date**: 2026-03-22
**Status**: Draft
**Author**: jndrdlx + Claude

## Problem Statement

Logistics companies need 24/7 monitoring of delivery routes and real-time communication with drivers. Human dispatchers are expensive, don't scale, and can't monitor hundreds of routes simultaneously. We need a digital workforce ("chalanes") that handles control tower operations autonomously — proactive anomaly detection, reactive incident handling via WhatsApp and email, and escalation to human supervisors when needed.

## Product Vision

Chalanes is a multi-tenant SaaS platform that sells fleets of digital employees to logistics companies. Each chalan is an autonomous AI agent specialized for a role (control tower, delivery analyst, payments analyst, etc.). Fleets are deployed per-tenant in isolated Kubernetes namespaces, backed by shared PostgreSQL, and orchestrated by a fleet coordinator.

## Architecture: Chalan-per-Pod with Fleet Coordinator

### Two-tier model

**Tier 1 — Fleet Coordinator** (1 per tenant):
- ZeroClaw instance with swarm and delegate tools
- Routes cross-chalan tasks (reassign routes, aggregate metrics)
- Health-checks all worker chalanes via cron
- Stores fleet-wide audit log in Postgres
- Exposes gateway API for future dashboard integration

**Tier 2 — Worker Chalanes** (N per tenant):
- One ZeroClaw instance per pod (~8-15 MB RAM)
- Each has its own config (persona, channels, tools, routes)
- Communicates with drivers via WhatsApp and/or email
- Monitors shipment tracking API via cron
- Escalates to fleet coordinator via delegate tool
- Shares Postgres schema with tenant fleet

### Why this architecture

- Worker chalanes are stateless processes + Postgres = easy horizontal scaling
- One pod dies, one chalan goes down, k8s restarts it in seconds
- Fleet coordinator handles cross-cutting concerns without inter-pod networking between workers
- Small tenants (5-50 chalanes) pack dense on 1-2 nodes
- Enterprise tenants (500-5,000) scale horizontally, coordinator can be HA
- Tenant isolation at k8s namespace level + Postgres schema level

## Chalan Archetypes

An archetype is a reusable role template: persona + tools + channels + behavior.

### Defined archetypes

| Archetype | Channels | Key Tools | Proactive Behavior | Escalates To |
|-----------|----------|-----------|-------------------|-------------|
| **fleet-coordinator** | Gateway API only | swarm, delegate, memory, http_request | Cron health-checks all chalanes, aggregates metrics | Human ops (Slack/email) |
| **control-tower** | WhatsApp, Email | http_request (tracking API), memory, delegate | Cron polls tracking API for anomalies, contacts drivers | fleet-coordinator |
| **delivery-analyst** | Email only | http_request (tracking + BI API), memory, delegate | Cron generates daily/weekly delivery performance reports | fleet-coordinator |
| **payments-analyst** | Email only | http_request (payments API, ERP), memory, delegate | Cron flags overdue invoices, reconciliation mismatches | fleet-coordinator |
| **customer-support** | WhatsApp, Email | http_request (tracking API), memory, delegate | None (reactive only) | control-tower chalan |

### Archetype structure

Each archetype is a TOML template (`archetypes/{name}.toml.tpl`) rendered with tenant-specific variables during provisioning. Templates use Handlebars-style placeholders for: tenant name, provider/model, API keys, channel credentials, tracking API domains, route assignments.

### Adding new archetypes

Write one `.toml.tpl` file. No code changes to ZeroClaw. Reference it in a fleet manifest.

## Per-Chalan Customization

Three layers, coarse to fine:

### Layer 1 — Config file (k8s ConfigMap)

Generated from archetype template + tenant variables. Controls: persona, system prompt, identity, channels, provider/model, tool allowlists, autonomy level, security policy, query classification rules, delegate agent configs.

Updated by: modify ConfigMap, rolling restart. ZeroClaw reads config at boot.

### Layer 2 — Memory (Postgres, persistent)

Accumulated knowledge that survives restarts:
- `core` — long-term facts about drivers, routes, preferences
- `daily` — session-scoped operational context
- `conversation` — per-thread context with each driver

### Layer 3 — Memory Snapshot (soul backup)

ZeroClaw auto-exports `MEMORY_SNAPSHOT.md` from core memories. If Postgres data is lost, the chalan auto-hydrates from this snapshot on next boot. Stored in PersistentVolume or synced to object storage.

## Tenant Provisioning

### Fleet manifest

Each tenant is defined by a YAML manifest:

```yaml
tenant: acme-logistics
provider: openrouter
api_key_secret: acme-api-key

chalanes:
  - archetype: fleet-coordinator
    count: 1
  - archetype: control-tower
    count: 12
    assign_to: routes
    channels:
      whatsapp: true
      email: true
  - archetype: delivery-analyst
    count: 2
    assign_to: regions
    channels:
      email: true
  - archetype: payments-analyst
    count: 1
    channels:
      email: true
    tools:
      http_request:
        allowed_domains:
          - api.acme.com
          - erp.acme.com
```

### Provisioning steps (`provision-tenant.sh`)

1. Create k8s namespace `chalanes-{tenant}`
2. Create Postgres schema `{tenant}` with standard tables
3. Create k8s Secret from tenant credentials
4. Render archetype templates into ConfigMaps (one per chalan)
5. Auto-generate coordinator config with swarm/delegate entries for all worker chalanes
6. Helm install into the namespace
7. Seed initial data (routes, drivers, shipments)

### Teardown

`kubectl delete namespace chalanes-{tenant}` + `DROP SCHEMA {tenant} CASCADE`.

## Data Model (PostgreSQL)

Per-tenant schema with these tables:

### `chalanes`
Fleet registry. Fields: id (UUID PK), name, role (worker|coordinator), status, config_hash (drift detection), last_heartbeat, created_at.

### `routes`
Route-to-chalan assignment. Fields: id (UUID PK), chalan_id (FK), route_code, origin, destination, driver_name, driver_phone (WhatsApp E.164), driver_email, status, metadata (JSONB).

### `incidents`
The chalan's work product. Fields: id (UUID PK), chalan_id (FK), route_id (FK), type (delay|breakdown|deviation|no_signal|resolved), severity (low|medium|high|critical), summary, source (whatsapp|email|tracker_api|proactive), resolved_at, created_at.

### `audit_log`
Every LLM call, tool use, message sent/received. Fields: id (UUID PK), chalan_id (FK), event_type, detail (JSONB), tokens_used, cost_usd, created_at.

### `memories`
ZeroClaw memory-postgres backend table. Fields: id (TEXT PK), key, content, category, created_at, updated_at, session_id. Indexed on category and session_id.

## Message & Incident Flow

### Reactive (driver-initiated)

```
Driver sends WhatsApp: "Flat tire on highway 45D km 230"
  → chalan-whatsapp receives via gateway webhook
  → Classifies: incident, severity=high, type=breakdown
  → Stores to incidents table
  → Queries tracking API: shipment ETA, cargo type, nearest alternate
  → Responds to driver: "Acknowledged. Roadside assistance dispatched. ETA 40min."
  → Delegates to coordinator: "Route MX-45 breakdown, need reassignment"
    → Coordinator queries routes: find available chalan with capacity
    → Delegates to chalan-mx03: "Take over shipment #4521"
    → Notifies ops via chalan-email
  → Logs full trace to audit_log
```

### Proactive (system-initiated)

```
Cron fires every 5 minutes on chalan-tracker
  → Queries tracking API for assigned routes
  → Detects: shipment #4580 has no GPS update in 18 minutes
  → Sends WhatsApp to driver via delegate to chalan-whatsapp: "We noticed your location hasn't updated. Everything OK?"
  → If no response in 10 minutes: escalate to coordinator
  → Logs to incidents table (type=no_signal, source=proactive)
```

## LLM Provider Strategy

Tenant-decides (BYOK):
- Provider and model configured per-tenant in fleet manifest
- Injected as k8s Secret, referenced in ConfigMap
- ZeroClaw's query classification + model routing enables per-tenant cost optimization:
  - Cheap model for ACKs and triage classification
  - Expensive model for incident resolution and multi-step coordination
- All token usage tracked in audit_log for billing

## Resilience

| Event | Behavior |
|-------|----------|
| Pod restart | Reads config from ConfigMap, reconnects to Postgres. Memory intact. Channels reconnect. Seconds. |
| Pod killed + rescheduled | Same as restart. Stateless binary + Postgres. |
| Postgres failure | Chalan continues running, channels still work. Memory ops fail gracefully. Alert to coordinator. |
| Config update | Update ConfigMap → rolling restart. New persona/tools/routes take effect. Memory persists. |
| Scale up | Add entries to fleet manifest, re-run provisioning or Helm upgrade. |

## Observability

Per-chalan:
- `/health` gateway endpoint — k8s liveness/readiness probes
- `channel health_check()` — per-channel connectivity
- `tracing` structured logs — JSON format for k8s log aggregation
- Cost tracker — per-model token usage, queryable via gateway API

Fleet-wide (via coordinator):
- Cron polling each chalan's `/health`
- Aggregated cost tracking in Postgres audit_log
- Incident counts, response times, escalation rates per chalan

## Local Experiment

### Project structure

```
chalanes-control-tower/
├── docker-compose.yml
├── configs/
│   ├── coordinator.toml
│   ├── chalan-whatsapp.toml
│   ├── chalan-email.toml
│   └── chalan-tracker.toml
├── archetypes/
│   ├── fleet-coordinator.toml.tpl
│   ├── control-tower.toml.tpl
│   ├── delivery-analyst.toml.tpl
│   └── payments-analyst.toml.tpl
├── mock-api/
│   └── server.py
├── postgres/
│   └── init.sql
├── scripts/
│   ├── provision-tenant.sh
│   └── seed-data.sh
├── k8s/
│   └── helm/
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
└── docs/
```

### Docker-compose topology

| Container | Role | Port |
|-----------|------|------|
| `postgres` | Shared memory + state | 5432 |
| `mock-api` | Shipment tracking simulation | 8000 |
| `mailhog` | Local SMTP/IMAP test server | 1025/1143/8025 |
| `coordinator` | Fleet brain | 8080 |
| `chalan-whatsapp` | WhatsApp handler | 8081 |
| `chalan-email` | Email handler | 8082 |
| `chalan-tracker` | Proactive route monitor | 8083 |

### Mock tracking API

FastAPI server with endpoints:
- `GET /shipments` — list (filterable by route, status, driver)
- `GET /shipments/{id}` — detail (location, ETA, status, driver)
- `GET /shipments/{id}/history` — location/status timeline
- `PATCH /shipments/{id}` — update status (simulate events)
- `POST /shipments/{id}/incidents` — report incident
- `GET /routes/{code}/shipments` — active shipments on a route
- `POST /simulate/tick` — advance simulation (random delays, GPS dropouts, deliveries)
- `GET /health` — API health

### Testing plan

| Test | Method | Success Criteria |
|------|--------|-----------------|
| Reactive WhatsApp | POST to chalan-whatsapp gateway simulating driver message | Classifies incident, logs to Postgres, responds |
| Reactive Email | Send email to MailHog, chalan-email picks it up | Parses, creates incident, replies |
| Proactive monitoring | Run mock-api simulation, let cron tick | Detects late shipment, sends WhatsApp, logs incident |
| Escalation | Trigger critical incident | Worker delegates to coordinator, coordinator routes |
| Memory persistence | Restart chalan pod, send follow-up | Remembers driver and prior context from Postgres |
| Fleet provisioning | Run provision-tenant.sh with manifest | Fleet comes up, health checks pass |
| Cost tracking | Run 10 interactions | Audit log shows token usage and cost per chalan |

## Out of Scope (Phase 1)

- Real WhatsApp Business API integration (use gateway mock)
- Real email provider (use MailHog)
- k8s Operator / CRD (use shell script + Helm)
- A2A protocol between chalanes (use delegate + Postgres)
- Dashboard / web UI for fleet management
- End-customer facing support (control-tower only, not customer-support archetype)
- Multi-region deployment
- SSO / RBAC for tenant management

## Future Phases

- **Phase 2**: Real WhatsApp + email integration, k8s Operator, dashboard
- **Phase 3**: A2A protocol for inter-chalan communication, customer-support archetype
- **Phase 4**: Multi-region, advanced analytics, tenant self-service portal
