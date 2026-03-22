# Chalanes Control Tower — Design Specification

**Date**: 2026-03-22
**Status**: Draft
**Author**: jndrdlx + Claude
**Review**: Passed spec review (15 findings addressed)

## Problem Statement

Logistics companies need 24/7 monitoring of delivery routes and real-time communication with drivers. Human dispatchers are expensive, don't scale, and can't monitor hundreds of routes simultaneously. We need a digital workforce ("chalanes") that handles control tower operations autonomously — proactive anomaly detection, reactive incident handling via WhatsApp and email, and escalation to human supervisors when needed.

## Product Vision

Chalanes is a multi-tenant SaaS platform that sells fleets of digital employees to logistics companies. Each chalan is an autonomous AI agent specialized for a role (control tower, delivery analyst, payments analyst, etc.). Fleets are deployed per-tenant in isolated Kubernetes namespaces, backed by shared PostgreSQL, and orchestrated by a fleet coordinator.

## Architecture: Chalan-per-Pod with Fleet Coordinator

### Two-tier model

**Tier 1 — Fleet Coordinator** (1 per tenant):
- ZeroClaw instance with `http_request` tool for cross-chalan coordination
- Routes cross-chalan tasks via Postgres task queue + gateway HTTP calls
- Health-checks all worker chalanes via cron (polling each chalan's `/health` gateway endpoint)
- Stores fleet-wide audit log in Postgres
- Exposes gateway API for future dashboard integration

**Tier 2 — Worker Chalanes** (N per tenant):
- One ZeroClaw instance per pod (~8-15 MB RAM)
- Each has its own config (persona, channels, tools, routes)
- Communicates with drivers via WhatsApp and/or email
- Monitors shipment tracking API via cron
- Escalates to fleet coordinator via `http_request` to coordinator's gateway API
- Uses Postgres for both memory and fleet tables (shared per-tenant schema)

### Inter-chalan communication model

ZeroClaw's `delegate` and `swarm` tools are **intra-process only** — they spawn sub-agents within the same ZeroClaw instance. Cross-pod communication uses two mechanisms:

1. **Postgres task queue** — coordinator writes task rows to a `task_queue` table; workers poll and pick up assigned tasks via cron. This is the primary coordination mechanism (durable, survives restarts).
2. **Gateway HTTP calls** — for urgent real-time signaling, chalanes use `http_request` to POST to another chalan's gateway `/api/message` endpoint. Requires `allow_private_hosts = true` and k8s service DNS in `allowed_domains`.

The `delegate` and `swarm` tools are still used **within** each chalan for internal multi-step reasoning (e.g., a control-tower chalan using delegate to run a classification sub-agent locally).

### Build requirements

All ZeroClaw container images **must** be built with `--features memory-postgres` to enable the Postgres memory backend. The standard ZeroClaw binary does not include this by default. The Dockerfile must specify:

```dockerfile
RUN cargo build --release --features memory-postgres
```

### Why this architecture

- Worker chalanes are stateless processes + Postgres = easy horizontal scaling
- One pod dies, one chalan goes down, k8s restarts it in seconds
- Fleet coordinator handles cross-cutting concerns via task queue (no direct inter-pod RPC needed)
- Small tenants (5-50 chalanes) pack dense on 1-2 nodes
- Enterprise tenants (500-5,000) scale horizontally, coordinator can be HA
- Tenant isolation at k8s namespace level + Postgres schema level + Postgres role-per-tenant

## Chalan Archetypes

An archetype is a reusable role template: persona + tools + channels + behavior.

### Defined archetypes

| Archetype | Channels | Key Tools | Proactive Behavior | Escalates To |
|-----------|----------|-----------|-------------------|-------------|
| **fleet-coordinator** | Gateway API only | memory, http_request | Cron health-checks all chalanes, polls task queue, aggregates metrics | Human ops (Slack/email) |
| **control-tower** | WhatsApp, Email | http_request (tracking API + coordinator gateway), memory | Cron polls tracking API for anomalies, contacts drivers directly | fleet-coordinator (via task queue) |
| **delivery-analyst** | Email only | http_request (tracking + BI API), memory | Cron generates daily/weekly delivery performance reports | fleet-coordinator (via task queue) |
| **payments-analyst** | Email only | http_request (payments API, ERP), memory | Cron flags overdue invoices, reconciliation mismatches | fleet-coordinator (via task queue) |
| **customer-support** | WhatsApp, Email | http_request (tracking API), memory | None (reactive only) | control-tower chalan (via task queue) |

### Archetype structure

Each archetype is a TOML template (`archetypes/{name}.toml.tpl`) rendered with tenant-specific variables during provisioning. Templates use Handlebars-style placeholders for: tenant name, provider/model, API keys, channel credentials, tracking API domains, route assignments.

All archetype templates must include:

```toml
[memory]
backend = "postgres"

[storage.provider.config]
provider = "postgres"
db_url = "{{postgres_url}}"
schema = "{{tenant_schema}}"

[http_request]
allow_private_hosts = true
allowed_domains = [
  "{{tracking_api_domain}}",
  "coordinator.chalanes-{{tenant}}.svc.cluster.local",
]
```

### Adding new archetypes

Write one `.toml.tpl` file. No code changes to ZeroClaw. Reference it in a fleet manifest.

## Per-Chalan Customization

Three layers, coarse to fine:

### Layer 1 — Config file (k8s ConfigMap)

Generated from archetype template + tenant variables. Controls: persona, system prompt, identity, channels, provider/model, tool allowlists, autonomy level, security policy, query classification rules.

Updated by: modify ConfigMap, rolling restart. ZeroClaw reads config at boot.

### Layer 2 — Memory (Postgres, shared and durable)

All chalanes use the Postgres memory backend (`backend = "postgres"`). Accumulated knowledge survives pod restarts, rescheduling, and node failures — the memories live in the database, not in the pod:
- `core` — long-term facts about drivers, routes, preferences
- `daily` — session-scoped operational context
- `conversation` — per-thread context with each driver

**No file-based soul backup needed.** ZeroClaw's `MEMORY_SNAPSHOT.md` auto-hydration mechanism exists for the SQLite backend (where `brain.db` is a local file that can be lost). With Postgres, the database itself is the durable store — pod crashes, rescheduling, and scaling events don't affect the data. Postgres replication/backups handle disaster recovery at the infrastructure level.

### Why Postgres for everything

- Single storage layer — no PersistentVolumes, no StatefulSets, simpler k8s topology (Deployments instead)
- Memories are immediately available on any pod (no PV reattach delay)
- Fleet-wide visibility — coordinator can query any chalan's memories if needed for cross-chalan context
- Postgres handles backup/replication natively — no custom snapshot mechanism
- Tenant isolation via schema + role — same model for memory tables and fleet tables
- Tradeoff: chalan depends on Postgres availability for memory ops (acceptable — fleet tables already require it)

## Tenant Provisioning

### Fleet manifest

Each tenant is defined by a YAML manifest:

```yaml
tenant: acme-logistics
provider: openrouter
api_key_secret: acme-api-key
tracking_api_domain: api.acme.com

whatsapp:
  mode: shared              # shared (one number, coordinator routes) or dedicated (one per chalan)
  phone_number_id: "123..."
  access_token_secret: acme-wa-token
  verify_token: "verify-..."

email:
  imap_host: imap.acme.com
  smtp_host: smtp.acme.com
  credentials_secret: acme-email-creds

chalanes:
  - archetype: fleet-coordinator
    count: 1

  - archetype: control-tower
    count: 12
    assign_to: routes       # maps to routes table: round-robin or explicit
    routes:                 # optional explicit mapping
      - "MX-45-CDMX-GDL"
      - "MX-46-CDMX-MTY"
      # ... remaining 10 auto-assigned from unassigned routes
    channels:
      whatsapp: true
      email: true

  - archetype: delivery-analyst
    count: 2
    assign_to: regions
    regions:
      - "north"
      - "south"
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

### WhatsApp number strategy

Two modes:

- **Shared** (default, recommended for cost): One WhatsApp Business number per tenant. All inbound messages hit the coordinator's gateway webhook. Coordinator looks up driver phone → assigned chalan in `routes` table, forwards to the correct chalan via task queue. Outbound messages from any chalan go through the shared number (each chalan has the access token).
- **Dedicated**: One WhatsApp number per chalan (expensive — requires N Business numbers). Each chalan receives its own webhooks directly. Only viable for enterprise tenants with Meta Business Manager approval for multiple numbers.

### Provisioning steps (`provision-tenant.sh`)

1. Create k8s namespace `chalanes-{tenant}`
2. Create Postgres role `chalanes_{tenant}` with `USAGE` and `CREATE` on schema `{tenant}` only (no cross-schema access)
3. Create Postgres schema `{tenant}` owned by the tenant role, with standard tables
4. Create k8s Secret from tenant credentials (API keys, WhatsApp token, email creds, Postgres role password)
5. Render archetype templates into ConfigMaps (one per chalan), injecting:
   - Tenant schema name, Postgres connection string (with tenant role)
   - Route/region assignments (from manifest `routes`/`regions` fields or auto-assigned from `routes` table)
   - k8s service DNS for coordinator endpoint
   - `allow_private_hosts = true` and `allowed_domains` including coordinator and mock-api
6. Helm install into the namespace
7. Seed initial data (routes, drivers, shipments) into tenant schema

### Route/region assignment logic

When `assign_to: routes`:
- If `routes` list is provided in manifest: assign explicitly (chalan-01 → route[0], chalan-02 → route[1], etc.)
- If `routes` is omitted: query `routes` table for unassigned routes, round-robin assign to chalanes
- Assignment is recorded in `routes.chalan_id` column

When `assign_to: regions`:
- The `regions` list maps 1:1 to chalan instances
- Each region chalan handles all routes in that region (determined by a `region` column in `routes` table)

### Teardown

`kubectl delete namespace chalanes-{tenant}` + `DROP SCHEMA {tenant} CASCADE` + `DROP ROLE chalanes_{tenant}`.

## Data Model (PostgreSQL)

Per-tenant schema with these tables (owned by tenant-specific Postgres role):

### `chalanes`
Fleet registry. Fields: id (UUID PK), name, archetype, role (worker|coordinator), status, config_hash (drift detection), last_heartbeat, created_at.

### `routes`
Route-to-chalan assignment. Fields: id (UUID PK), chalan_id (FK), route_code, region, origin, destination, driver_name, driver_phone (WhatsApp E.164), driver_email, status, metadata (JSONB).

### `incidents`
The chalan's work product. Fields: id (UUID PK), chalan_id (FK), route_id (FK), type (delay|breakdown|deviation|no_signal|resolved), severity (low|medium|high|critical), summary, source (whatsapp|email|tracker_api|proactive), resolved_at, created_at.

### `task_queue`
Cross-chalan coordination. Fields: id (UUID PK), from_chalan_id (FK), to_chalan_id (FK, nullable for coordinator), action (TEXT — e.g., "reassign_route", "notify_driver", "escalate"), payload (JSONB), status (pending|claimed|completed|failed), claimed_at, completed_at, created_at.

Workers poll this table via cron (every 30s) for tasks assigned to them. Coordinator polls for escalations.

### `audit_log`
Every LLM call, tool use, message sent/received. Fields: id (UUID PK), chalan_id (FK), event_type, detail (JSONB), tokens_used, cost_usd, created_at.

## Message & Incident Flow

### Reactive (driver-initiated)

```
Driver sends WhatsApp: "Flat tire on highway 45D km 230"
  → Gateway webhook receives on shared WhatsApp number
  → Coordinator looks up driver phone in routes table → assigned to chalan-mx01
  → Coordinator writes task_queue entry: {to: chalan-mx01, action: "handle_driver_message", payload: {...}}
  → chalan-mx01 picks up task on next cron poll (≤30s)
  → Classifies: incident, severity=high, type=breakdown
  → Stores to incidents table
  → Queries tracking API via http_request: shipment ETA, cargo type
  → Sends WhatsApp reply to driver (outbound via shared number access token)
  → Writes escalation to task_queue: {to: coordinator, action: "reassign_route", payload: {route: "MX-45", reason: "breakdown"}}
  → Coordinator picks up escalation, queries routes for available chalan
  → Coordinator writes task: {to: chalan-mx03, action: "take_over_shipment", payload: {shipment: "#4521"}}
  → Coordinator writes task: {to: chalan-email, action: "notify_ops", payload: {subject: "Route MX-45 breakdown"}}
  → All steps logged to audit_log
```

### Proactive (system-initiated)

```
Cron fires every 5 minutes on chalan-mx01 (control-tower archetype)
  → Queries tracking API for assigned routes via http_request
  → Detects: shipment #4580 has no GPS update in 18 minutes
  → Sends WhatsApp to driver directly (has access token): "We noticed your location hasn't updated. Everything OK?"
  → Stores incident in incidents table (type=no_signal, source=proactive)
  → If no response in 10 minutes (checked on next cron tick):
    → Writes escalation to task_queue: {to: coordinator, action: "escalate_no_signal", payload: {...}}
  → Logs to audit_log
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
| Pod restart | Reads config from ConfigMap, reconnects to Postgres. All memory intact in database. Channels reconnect. Seconds. |
| Pod killed + rescheduled to new node | No local state to lose — memory is in Postgres. New pod connects and has full context immediately. No PV needed. |
| Postgres unavailable at startup | Chalan fails to start (Postgres is required for both memory and fleet tables). k8s restarts until Postgres recovers. Configure `connect_timeout_secs` to bound wait time. |
| Postgres fails mid-session | Channel listeners continue but memory ops and fleet table writes produce errors surfaced to the LLM. Chalan can still receive messages but cannot recall context or log incidents until recovery. |
| Config update | Update ConfigMap → rolling restart. New persona/tools/routes take effect. Memory persists in Postgres. |
| Scale up | Add entries to fleet manifest, re-run provisioning or Helm upgrade. |

## Security

### Tenant isolation

- **k8s namespace** per tenant — NetworkPolicy restricts cross-namespace traffic
- **Postgres role** per tenant — role can only access its own schema, no cross-schema queries
- **k8s Secrets** for all credentials — never in ConfigMaps or config files

### Network security

- **Postgres TLS**: ZeroClaw's Postgres backend currently connects with `NoTls`. For Phase 1 (local experiment), this is acceptable. For production, either:
  - (a) Deploy a service mesh (Istio/Linkerd) for mTLS on all intra-cluster traffic, or
  - (b) Contribute TLS support to ZeroClaw's Postgres backend (upstream issue required)
- **NetworkPolicy**: Required in Helm chart to restrict:
  - Chalan pods can only reach: Postgres, coordinator, mock-api (within same namespace)
  - `/health` endpoint accessible only from same namespace + kubelet CIDR
  - No cross-namespace pod-to-pod traffic
- **http_request**: `allow_private_hosts = true` scoped to specific `allowed_domains` (coordinator DNS, tracking API). No wildcard.

### Credential rotation

- k8s Secret rotation for API keys and WhatsApp tokens — rolling restart picks up new values
- Postgres role password rotation — update Secret + restart

## Observability

Per-chalan:
- `/health` gateway endpoint — k8s liveness/readiness probes
- `channel health_check()` — per-channel connectivity
- `tracing` structured logs — JSON format for k8s log aggregation (stdout)
- Cost tracker — per-model token usage, queryable via gateway API

Fleet-wide (via coordinator):
- Cron polling each chalan's `/health` endpoint via `http_request`
- Aggregated cost tracking in Postgres audit_log
- Incident counts, response times, escalation rates per chalan (queryable from incidents table)
- Task queue depth monitoring (detect stuck/slow chalanes)

## Local Experiment

### Project structure

```
chalanes-control-tower/
├── docker-compose.yml
├── Dockerfile.zeroclaw           # Builds ZeroClaw with --features memory-postgres
├── configs/
│   ├── coordinator.toml
│   └── chalan-control-tower.toml # Single control-tower chalan for the experiment
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
│           ├── coordinator.yaml
│           ├── chalan-deployment.yaml
│           ├── postgres.yaml
│           ├── secrets.yaml
│           └── networkpolicy.yaml
└── docs/
```

### Docker-compose topology

The local experiment collapses the architecture into fewer containers for simplicity. Instead of separate WhatsApp/email/tracker containers (which can't cross-delegate anyway), each control-tower chalan is a **single container** with WhatsApp + email + cron all configured.

| Container | Role | Port |
|-----------|------|------|
| `postgres` | Fleet tables + shared state | 5432 |
| `mock-api` | Shipment tracking simulation | 8000 |
| `mailpit` | Local SMTP + IMAP test server (replaces MailHog — supports IMAP IDLE) | 1025 (SMTP) / 1143 (IMAP) / 8025 (Web UI) |
| `coordinator` | Fleet brain, task queue processor, WhatsApp webhook receiver | 8080 |
| `chalan-01` | Control-tower worker (WhatsApp outbound + email + cron tracking) | 8081 |
| `chalan-02` | Control-tower worker (WhatsApp outbound + email + cron tracking) | 8082 |

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
| Reactive WhatsApp | POST to coordinator gateway simulating WhatsApp webhook | Coordinator routes to chalan-01 via task_queue, chalan-01 classifies and responds |
| Reactive Email | Send email to Mailpit, chalan-01 picks it up via IMAP IDLE | Parses, creates incident in Postgres, replies via SMTP |
| Proactive monitoring | Run `POST /simulate/tick` on mock-api, wait for chalan-01 cron | Detects late shipment, sends WhatsApp, logs incident |
| Escalation via task queue | Trigger critical incident on chalan-01 | chalan-01 writes to task_queue, coordinator picks up and routes |
| Memory persistence | Restart chalan-01 container, send follow-up | Remembers driver and prior context from Postgres |
| Fleet provisioning | Run provision-tenant.sh with manifest | Coordinator + workers come up, all /health checks pass |
| Cost tracking | Run 10 interactions | audit_log shows token usage and cost per chalan |

## Out of Scope (Phase 1)

- Real WhatsApp Business API integration (use gateway mock for inbound, direct API for outbound in experiment)
- Real email provider (use Mailpit)
- k8s Operator / CRD (use shell script + Helm)
- A2A protocol between chalanes (use task queue + http_request)
- Dashboard / web UI for fleet management
- End-customer facing support (control-tower only)
- Multi-region deployment
- SSO / RBAC for tenant management
- Postgres TLS (use plaintext for local experiment; service mesh or upstream fix for production)
- XOAUTH2 for Gmail Enterprise (use App Passwords for Phase 1; upstream contribution for OAuth2 IMAP/SMTP auth in Phase 2)

## Future Phases

- **Phase 2**: Real WhatsApp + email integration (including XOAUTH2 for Gmail Enterprise), k8s Operator, dashboard, Postgres TLS
- **Phase 3**: A2A protocol for inter-chalan communication (when ZeroClaw PR #4166 merges), customer-support archetype
- **Phase 4**: Multi-region, advanced analytics, tenant self-service portal
