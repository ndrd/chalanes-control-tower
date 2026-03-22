# Chalanes Control Tower — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a local multi-agent swarm experiment — fleet coordinator + 2 control-tower chalanes + mock tracking API + Postgres + Mailpit — all via docker-compose.

**Architecture:** Multiple ZeroClaw containers sharing a Postgres database for memory and fleet tables. Coordinator receives inbound events and routes tasks via a `task_queue` table. Worker chalanes poll the queue and handle driver communication. Mock API simulates shipment tracking with anomaly injection.

**Tech Stack:** ZeroClaw (Rust, existing Dockerfile), FastAPI (Python, mock API), PostgreSQL 17, Mailpit (IMAP/SMTP test server), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-03-22-chalanes-control-tower-design.md`

---

## File Structure

```
chalanes-control-tower/
├── docker-compose.yml                  # Orchestrates all 6 services
├── postgres/
│   └── init.sql                        # Schema: chalanes, routes, incidents, task_queue, audit_log, memories
├── mock-api/
│   ├── server.py                       # FastAPI shipment tracking mock
│   ├── requirements.txt                # fastapi, uvicorn
│   └── Dockerfile                      # Python 3.12-slim
├── configs/
│   ├── coordinator.toml                # Fleet coordinator ZeroClaw config
│   ├── chalan-01.toml                  # Control-tower worker 1
│   └── chalan-02.toml                  # Control-tower worker 2
├── scripts/
│   ├── seed-data.sh                    # Insert mock routes, drivers, shipments
│   └── test-flows.sh                   # Smoke tests for all 7 test scenarios
└── docs/
    └── superpowers/
        ├── specs/
        │   └── 2026-03-22-chalanes-control-tower-design.md
        └── plans/
            └── 2026-03-22-chalanes-control-tower-plan.md
```

---

### Task 1: Postgres Schema

**Files:**
- Create: `postgres/init.sql`

- [ ] **Step 1: Write init.sql with all fleet tables**

```sql
-- postgres/init.sql
-- Chalanes Control Tower — experiment tenant schema

CREATE SCHEMA IF NOT EXISTS experiment;

-- Fleet registry
CREATE TABLE experiment.chalanes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    archetype   TEXT NOT NULL DEFAULT 'control-tower',
    role        TEXT NOT NULL DEFAULT 'worker',
    status      TEXT NOT NULL DEFAULT 'active',
    config_hash TEXT,
    last_heartbeat TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Routes with driver assignment
CREATE TABLE experiment.routes (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chalan_id    UUID REFERENCES experiment.chalanes(id),
    route_code   TEXT NOT NULL,
    region       TEXT DEFAULT 'central',
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    driver_name  TEXT,
    driver_phone TEXT,
    driver_email TEXT,
    status       TEXT DEFAULT 'active',
    metadata     JSONB DEFAULT '{}',
    created_at   TIMESTAMPTZ DEFAULT now()
);

-- Incidents (chalan work product)
CREATE TABLE experiment.incidents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chalan_id   UUID REFERENCES experiment.chalanes(id),
    route_id    UUID REFERENCES experiment.routes(id),
    type        TEXT NOT NULL,
    severity    TEXT DEFAULT 'medium',
    summary     TEXT NOT NULL,
    source      TEXT NOT NULL,
    resolved_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Cross-chalan task queue
CREATE TABLE experiment.task_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_chalan_id  UUID REFERENCES experiment.chalanes(id),
    to_chalan_id    UUID REFERENCES experiment.chalanes(id),
    action          TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending',
    claimed_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_task_queue_status ON experiment.task_queue(status, to_chalan_id);

-- Audit log
CREATE TABLE experiment.audit_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chalan_id   UUID REFERENCES experiment.chalanes(id),
    event_type  TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}',
    tokens_used INTEGER,
    cost_usd    NUMERIC(10,6),
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- ZeroClaw memory-postgres backend table
-- (ZeroClaw creates this automatically, but we pre-create in the correct schema)
CREATE TABLE experiment.memories (
    id          TEXT PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    content     TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'core',
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    session_id  TEXT
);
CREATE INDEX idx_memories_category ON experiment.memories(category);
CREATE INDEX idx_memories_session_id ON experiment.memories(session_id);
```

- [ ] **Step 2: Verify SQL syntax locally**

Run: `psql -h localhost -U postgres -f postgres/init.sql 2>&1 | tail -5`
Expected: CREATE TABLE / CREATE INDEX statements succeed (or "already exists" if re-run).

- [ ] **Step 3: Commit**

```bash
git add postgres/init.sql
git commit -m "feat: add Postgres schema for experiment tenant"
```

---

### Task 2: Mock Tracking API

**Files:**
- Create: `mock-api/requirements.txt`
- Create: `mock-api/server.py`
- Create: `mock-api/Dockerfile`

- [ ] **Step 1: Create requirements.txt**

```
fastapi==0.115.12
uvicorn[standard]==0.34.2
```

- [ ] **Step 2: Write mock API server**

```python
# mock-api/server.py
"""Shipment tracking mock API with simulation mode."""

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Chalanes Mock Tracking API")

# ── In-memory state ──────────────────────────────────────────

SHIPMENTS: dict[str, dict] = {}
ROUTES: dict[str, list[str]] = {}  # route_code → [shipment_ids]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_shipments() -> None:
    """Seed initial shipments for 4 routes, 3 shipments each."""
    routes = [
        ("MX-45-CDMX-GDL", "CDMX", "Guadalajara"),
        ("MX-46-CDMX-MTY", "CDMX", "Monterrey"),
        ("MX-47-GDL-TIJ", "Guadalajara", "Tijuana"),
        ("MX-48-MTY-MER", "Monterrey", "Merida"),
    ]
    drivers = [
        ("Juan Perez", "+5215512345001", "juan@drivers.test"),
        ("Maria Lopez", "+5215512345002", "maria@drivers.test"),
        ("Carlos Ruiz", "+5215512345003", "carlos@drivers.test"),
        ("Ana Torres", "+5215512345004", "ana@drivers.test"),
    ]

    for i, (route_code, origin, dest) in enumerate(routes):
        ROUTES[route_code] = []
        driver = drivers[i]
        for j in range(3):
            sid = str(uuid.uuid4())[:8]
            eta_hours = random.uniform(2, 12)
            shipment = {
                "id": sid,
                "route_code": route_code,
                "origin": origin,
                "destination": dest,
                "status": "in_transit",
                "driver_name": driver[0],
                "driver_phone": driver[1],
                "driver_email": driver[2],
                "eta": (
                    datetime.now(timezone.utc) + timedelta(hours=eta_hours)
                ).isoformat(),
                "last_gps_update": _now(),
                "latitude": 19.4326 + random.uniform(-2, 2),
                "longitude": -99.1332 + random.uniform(-2, 2),
                "cargo_type": random.choice(
                    ["electronics", "food_perishable", "industrial", "retail"]
                ),
                "weight_kg": random.randint(500, 15000),
                "history": [
                    {
                        "timestamp": _now(),
                        "event": "departed",
                        "location": origin,
                    }
                ],
            }
            SHIPMENTS[sid] = shipment
            ROUTES[route_code].append(sid)


_seed_shipments()


# ── Endpoints ────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok", "shipments": len(SHIPMENTS)}


@app.get("/shipments")
def list_shipments(
    route: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    driver_phone: Optional[str] = Query(None),
):
    results = list(SHIPMENTS.values())
    if route:
        results = [s for s in results if s["route_code"] == route]
    if status:
        results = [s for s in results if s["status"] == status]
    if driver_phone:
        results = [s for s in results if s["driver_phone"] == driver_phone]
    return {"shipments": results, "total": len(results)}


@app.get("/shipments/{shipment_id}")
def get_shipment(shipment_id: str):
    if shipment_id not in SHIPMENTS:
        raise HTTPException(404, f"Shipment not found")
    return SHIPMENTS[shipment_id]


@app.get("/shipments/{shipment_id}/history")
def get_history(shipment_id: str):
    if shipment_id not in SHIPMENTS:
        raise HTTPException(404, f"Shipment not found")
    return {"shipment_id": shipment_id, "history": SHIPMENTS[shipment_id]["history"]}


@app.patch("/shipments/{shipment_id}")
def update_shipment(shipment_id: str, update: dict):
    if shipment_id not in SHIPMENTS:
        raise HTTPException(404, f"Shipment not found")
    allowed = {"status", "latitude", "longitude", "last_gps_update", "eta"}
    for key in update:
        if key in allowed:
            SHIPMENTS[shipment_id][key] = update[key]
    SHIPMENTS[shipment_id]["history"].append(
        {"timestamp": _now(), "event": "updated", "fields": list(update.keys())}
    )
    return SHIPMENTS[shipment_id]


@app.post("/shipments/{shipment_id}/incidents")
def report_incident(shipment_id: str, incident: dict):
    if shipment_id not in SHIPMENTS:
        raise HTTPException(404, f"Shipment not found")
    event = {
        "timestamp": _now(),
        "event": "incident",
        "type": incident.get("type", "unknown"),
        "description": incident.get("description", ""),
    }
    SHIPMENTS[shipment_id]["history"].append(event)
    if incident.get("type") == "breakdown":
        SHIPMENTS[shipment_id]["status"] = "stopped"
    return {"recorded": True, "event": event}


@app.get("/routes/{route_code}/shipments")
def route_shipments(route_code: str):
    sids = ROUTES.get(route_code, [])
    shipments = [SHIPMENTS[sid] for sid in sids if sid in SHIPMENTS]
    return {"route_code": route_code, "shipments": shipments, "total": len(shipments)}


@app.post("/simulate/tick")
def simulate_tick():
    """Advance simulation: random delays, GPS dropouts, deliveries."""
    events = []
    for sid, s in SHIPMENTS.items():
        if s["status"] != "in_transit":
            continue

        roll = random.random()

        # 5% chance: delay
        if roll < 0.05:
            new_eta = (
                datetime.fromisoformat(s["eta"]) + timedelta(hours=2)
            ).isoformat()
            s["eta"] = new_eta
            s["history"].append(
                {"timestamp": _now(), "event": "delayed", "new_eta": new_eta}
            )
            events.append({"shipment": sid, "event": "delayed"})

        # 3% chance: GPS dropout (last_gps_update goes stale)
        elif roll < 0.08:
            s["last_gps_update"] = (
                datetime.now(timezone.utc) - timedelta(minutes=20)
            ).isoformat()
            events.append({"shipment": sid, "event": "gps_dropout"})

        # 2% chance: route deviation
        elif roll < 0.10:
            s["latitude"] += random.uniform(-0.5, 0.5)
            s["longitude"] += random.uniform(-0.5, 0.5)
            s["history"].append(
                {"timestamp": _now(), "event": "route_deviation"}
            )
            events.append({"shipment": sid, "event": "route_deviation"})

        # 10% chance: delivered
        elif roll < 0.20:
            s["status"] = "delivered"
            s["history"].append(
                {
                    "timestamp": _now(),
                    "event": "delivered",
                    "location": s["destination"],
                }
            )
            events.append({"shipment": sid, "event": "delivered"})

        # Rest: normal progress (update GPS)
        else:
            s["last_gps_update"] = _now()
            s["latitude"] += random.uniform(-0.01, 0.01)
            s["longitude"] += random.uniform(-0.01, 0.01)

    return {"tick": _now(), "events": events, "total_events": len(events)}
```

- [ ] **Step 3: Write Dockerfile for mock API**

```dockerfile
# mock-api/Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 4: Test mock API locally**

Run: `cd mock-api && pip install -r requirements.txt && uvicorn server:app --port 8000 &`
Then: `curl -s http://localhost:8000/health | python3 -m json.tool`
Expected: `{"status": "ok", "shipments": 12}`
Then: `curl -s -X POST http://localhost:8000/simulate/tick | python3 -m json.tool`
Expected: JSON with `tick`, `events` array, `total_events` count.
Cleanup: `kill %1`

- [ ] **Step 5: Commit**

```bash
git add mock-api/
git commit -m "feat: add mock shipment tracking API with simulation"
```

---

### Task 3: ZeroClaw Configs

**Files:**
- Create: `configs/coordinator.toml`
- Create: `configs/chalan-01.toml`
- Create: `configs/chalan-02.toml`

- [ ] **Step 1: Write coordinator config**

```toml
# configs/coordinator.toml
# Fleet Coordinator — routes tasks, health-checks workers

workspace_dir = "/zeroclaw-data/workspace"
persona = "Fleet Coordinator for experiment tenant. You manage a team of control-tower chalanes. Route driver messages to the correct chalan, handle escalations, and monitor fleet health."

[defaults]
default_provider = "openrouter"
model = "anthropic/claude-sonnet-4-20250514"

[memory]
backend = "postgres"

[storage.provider.config]
provider = "postgres"
db_url = "postgres://postgres:postgres@postgres:5432/postgres"
schema = "experiment"
table = "memories"

[gateway]
port = 42617
host = "0.0.0.0"
allow_public_bind = true

[http_request]
allow_private_hosts = true
allowed_domains = ["mock-api", "chalan-01", "chalan-02"]

[autonomy]
level = "full"
```

- [ ] **Step 2: Write chalan-01 config**

```toml
# configs/chalan-01.toml
# Control Tower Worker 1 — routes MX-45, MX-46

workspace_dir = "/zeroclaw-data/workspace"
persona = "Control Tower Chalan-01. You monitor delivery routes MX-45-CDMX-GDL and MX-46-CDMX-MTY. Communicate with drivers via WhatsApp and email. Detect anomalies proactively. Escalate critical incidents to the coordinator."

[defaults]
default_provider = "openrouter"
model = "anthropic/claude-sonnet-4-20250514"

[memory]
backend = "postgres"

[storage.provider.config]
provider = "postgres"
db_url = "postgres://postgres:postgres@postgres:5432/postgres"
schema = "experiment"
table = "memories"

[gateway]
port = 42617
host = "0.0.0.0"
allow_public_bind = true

[channels.email]
imap_host = "mailpit"
imap_port = 1143
smtp_host = "mailpit"
smtp_port = 1025
smtp_tls = false
username = "chalan01@experiment.test"
password = "anything"
from_address = "chalan01@experiment.test"
allowed_senders = ["*"]
default_subject = "Chalan-01 Control Tower"

[http_request]
allow_private_hosts = true
allowed_domains = ["mock-api", "coordinator"]

[autonomy]
level = "full"
```

- [ ] **Step 3: Write chalan-02 config (same structure, different routes)**

```toml
# configs/chalan-02.toml
# Control Tower Worker 2 — routes MX-47, MX-48

workspace_dir = "/zeroclaw-data/workspace"
persona = "Control Tower Chalan-02. You monitor delivery routes MX-47-GDL-TIJ and MX-48-MTY-MER. Communicate with drivers via WhatsApp and email. Detect anomalies proactively. Escalate critical incidents to the coordinator."

[defaults]
default_provider = "openrouter"
model = "anthropic/claude-sonnet-4-20250514"

[memory]
backend = "postgres"

[storage.provider.config]
provider = "postgres"
db_url = "postgres://postgres:postgres@postgres:5432/postgres"
schema = "experiment"
table = "memories"

[gateway]
port = 42617
host = "0.0.0.0"
allow_public_bind = true

[channels.email]
imap_host = "mailpit"
imap_port = 1143
smtp_host = "mailpit"
smtp_port = 1025
smtp_tls = false
username = "chalan02@experiment.test"
password = "anything"
from_address = "chalan02@experiment.test"
allowed_senders = ["*"]
default_subject = "Chalan-02 Control Tower"

[http_request]
allow_private_hosts = true
allowed_domains = ["mock-api", "coordinator"]

[autonomy]
level = "full"
```

- [ ] **Step 4: Commit**

```bash
git add configs/
git commit -m "feat: add ZeroClaw configs for coordinator and 2 workers"
```

---

### Task 4: Seed Data Script

**Files:**
- Create: `scripts/seed-data.sh`

- [ ] **Step 1: Write seed script**

```bash
#!/usr/bin/env bash
# scripts/seed-data.sh — Seed experiment tenant with routes and drivers
set -euo pipefail

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGPASSWORD="${PGPASSWORD:-postgres}"
PGDATABASE="${PGDATABASE:-postgres}"

export PGPASSWORD

psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" <<'SQL'
-- Register chalanes
INSERT INTO experiment.chalanes (name, archetype, role) VALUES
  ('coordinator', 'fleet-coordinator', 'coordinator'),
  ('chalan-01', 'control-tower', 'worker'),
  ('chalan-02', 'control-tower', 'worker')
ON CONFLICT (name) DO NOTHING;

-- Assign routes to workers
WITH c1 AS (SELECT id FROM experiment.chalanes WHERE name = 'chalan-01'),
     c2 AS (SELECT id FROM experiment.chalanes WHERE name = 'chalan-02')
INSERT INTO experiment.routes (chalan_id, route_code, region, origin, destination, driver_name, driver_phone, driver_email) VALUES
  ((SELECT id FROM c1), 'MX-45-CDMX-GDL', 'central', 'CDMX', 'Guadalajara', 'Juan Perez', '+5215512345001', 'juan@drivers.test'),
  ((SELECT id FROM c1), 'MX-46-CDMX-MTY', 'central', 'CDMX', 'Monterrey', 'Maria Lopez', '+5215512345002', 'maria@drivers.test'),
  ((SELECT id FROM c2), 'MX-47-GDL-TIJ', 'north', 'Guadalajara', 'Tijuana', 'Carlos Ruiz', '+5215512345003', 'carlos@drivers.test'),
  ((SELECT id FROM c2), 'MX-48-MTY-MER', 'south', 'Monterrey', 'Merida', 'Ana Torres', '+5215512345004', 'ana@drivers.test')
ON CONFLICT DO NOTHING;

SELECT 'Seeded ' || count(*) || ' chalanes' FROM experiment.chalanes;
SELECT 'Seeded ' || count(*) || ' routes' FROM experiment.routes;
SQL
```

- [ ] **Step 2: Make executable**

Run: `chmod +x scripts/seed-data.sh`

- [ ] **Step 3: Commit**

```bash
git add scripts/seed-data.sh
git commit -m "feat: add seed data script for experiment tenant"
```

---

### Task 5: Docker Compose

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: Write docker-compose.yml**

```yaml
# docker-compose.yml — Chalanes Control Tower local experiment
services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_PASSWORD: postgres
    ports:
      - "5433:5432"  # Avoid conflict with host Postgres on 5432
    volumes:
      - ./postgres/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 3s
      retries: 5

  mock-api:
    build: ./mock-api
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 3s
      retries: 3

  mailpit:
    image: axllent/mailpit:latest
    ports:
      - "1025:1025"   # SMTP
      - "8025:8025"   # Web UI
      - "1143:1143"   # IMAP
    environment:
      MP_SMTP_AUTH_ACCEPT_ANY: "true"
      MP_SMTP_AUTH_ALLOW_INSECURE: "true"

  coordinator:
    build:
      context: ../zeroclaw
      dockerfile: Dockerfile
      target: dev
    ports:
      - "8080:42617"
    volumes:
      - ./configs/coordinator.toml:/zeroclaw-data/.zeroclaw/config.toml:ro
    environment:
      API_KEY: "${OPENROUTER_API_KEY:-}"
    depends_on:
      postgres:
        condition: service_healthy
      mock-api:
        condition: service_healthy

  chalan-01:
    build:
      context: ../zeroclaw
      dockerfile: Dockerfile
      target: dev
    ports:
      - "8081:42617"
    volumes:
      - ./configs/chalan-01.toml:/zeroclaw-data/.zeroclaw/config.toml:ro
    environment:
      API_KEY: "${OPENROUTER_API_KEY:-}"
    depends_on:
      postgres:
        condition: service_healthy
      mock-api:
        condition: service_healthy
      mailpit:
        condition: service_started

  chalan-02:
    build:
      context: ../zeroclaw
      dockerfile: Dockerfile
      target: dev
    ports:
      - "8082:42617"
    volumes:
      - ./configs/chalan-02.toml:/zeroclaw-data/.zeroclaw/config.toml:ro
    environment:
      API_KEY: "${OPENROUTER_API_KEY:-}"
    depends_on:
      postgres:
        condition: service_healthy
      mock-api:
        condition: service_healthy
      mailpit:
        condition: service_started

volumes:
  pgdata:
```

- [ ] **Step 2: Create .env file for API key**

Run: `echo 'OPENROUTER_API_KEY=your-key-here' > .env && echo '.env' >> .gitignore`

User must replace `your-key-here` with their actual OpenRouter API key.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml .gitignore
git commit -m "feat: add docker-compose for local experiment (6 services)"
```

---

### Task 6: Build and Boot

- [ ] **Step 1: Build all images**

Run: `cd /Users/jndrdlx/chalanes.ai/sbx/chalanes-control-tower && docker compose build`

Expected: ZeroClaw image builds from `../zeroclaw/Dockerfile` (this takes a few minutes the first time — Rust compilation). Mock API image builds in seconds.

- [ ] **Step 2: Start infrastructure services first**

Run: `docker compose up -d postgres mock-api mailpit`
Then: `docker compose ps`

Expected: All 3 services `healthy` or `running`.

- [ ] **Step 3: Seed the database**

Run: `PGHOST=localhost PGPORT=5433 ./scripts/seed-data.sh`

Expected:
```
Seeded 3 chalanes
Seeded 4 routes
```

- [ ] **Step 4: Start ZeroClaw fleet**

Run: `docker compose up -d coordinator chalan-01 chalan-02`
Then: `sleep 10 && docker compose ps`

Expected: All 6 services running. ZeroClaw containers show `healthy` after ~60s (healthcheck interval).

- [ ] **Step 5: Verify health endpoints**

Run:
```bash
curl -s http://localhost:8080/health | python3 -m json.tool  # coordinator
curl -s http://localhost:8081/health | python3 -m json.tool  # chalan-01
curl -s http://localhost:8082/health | python3 -m json.tool  # chalan-02
curl -s http://localhost:8000/health | python3 -m json.tool  # mock-api
```

Expected: All return `{"status": "ok", ...}` or equivalent.

- [ ] **Step 6: Verify Mailpit is reachable**

Open: `http://localhost:8025` in browser.
Expected: Mailpit web UI loads, empty inbox.

- [ ] **Step 7: Commit any adjustments**

```bash
git add -A
git commit -m "chore: adjustments from first boot" --allow-empty
```

---

### Task 7: Smoke Test Script

**Files:**
- Create: `scripts/test-flows.sh`

- [ ] **Step 1: Write smoke test script**

```bash
#!/usr/bin/env bash
# scripts/test-flows.sh — Smoke tests for the chalanes experiment
set -euo pipefail

COORDINATOR="http://localhost:8080"
CHALAN_01="http://localhost:8081"
CHALAN_02="http://localhost:8082"
MOCK_API="http://localhost:8000"
PG="psql -h localhost -p 5433 -U postgres -d postgres -t -A"

export PGPASSWORD=postgres

pass=0
fail=0

check() {
  local name="$1" cmd="$2" expected="$3"
  result=$(eval "$cmd" 2>&1) || true
  if echo "$result" | grep -q "$expected"; then
    echo "  PASS: $name"
    ((pass++))
  else
    echo "  FAIL: $name"
    echo "    Expected: $expected"
    echo "    Got: $result"
    ((fail++))
  fi
}

echo "=== Chalanes Control Tower Smoke Tests ==="
echo ""

echo "1. Health checks"
check "mock-api health" "curl -sf $MOCK_API/health" '"status":"ok"'
check "coordinator health" "curl -sf $COORDINATOR/health" 'status'
check "chalan-01 health" "curl -sf $CHALAN_01/health" 'status'
check "chalan-02 health" "curl -sf $CHALAN_02/health" 'status'

echo ""
echo "2. Mock API data"
check "shipments loaded" "curl -sf $MOCK_API/shipments | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"total\"])'" '12'
check "route MX-45 has shipments" "curl -sf '$MOCK_API/routes/MX-45-CDMX-GDL/shipments' | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"total\"])'" '3'

echo ""
echo "3. Simulation tick"
check "simulate produces events" "curl -sf -X POST $MOCK_API/simulate/tick | python3 -c 'import sys,json; d=json.load(sys.stdin); print(\"tick\" if \"tick\" in d else \"no\")'" 'tick'

echo ""
echo "4. Postgres schema"
check "chalanes table seeded" "$PG -c 'SELECT count(*) FROM experiment.chalanes'" '3'
check "routes table seeded" "$PG -c 'SELECT count(*) FROM experiment.routes'" '4'
check "task_queue exists" "$PG -c 'SELECT count(*) FROM experiment.task_queue'" '0'

echo ""
echo "5. Mailpit SMTP"
check "mailpit reachable" "curl -sf http://localhost:8025/api/v1/messages | python3 -c 'import sys,json; print(\"ok\")'" 'ok'

echo ""
echo "=== Results: $pass passed, $fail failed ==="
[ "$fail" -eq 0 ] && exit 0 || exit 1
```

- [ ] **Step 2: Make executable and run**

Run: `chmod +x scripts/test-flows.sh && ./scripts/test-flows.sh`

Expected: All checks PASS.

- [ ] **Step 3: Commit**

```bash
git add scripts/test-flows.sh
git commit -m "feat: add smoke test script for local experiment"
```

---

### Task 8: Send First Message to a Chalan

This is the manual integration test — send a message to chalan-01 via the gateway API and verify it processes.

- [ ] **Step 1: Send a test message to chalan-01**

Run:
```bash
curl -s -X POST http://localhost:8081/api/message \
  -H "Content-Type: application/json" \
  -d '{"message": "Flat tire on highway 45D km 230. Shipment delayed.", "sender": "driver-juan"}' \
  | python3 -m json.tool
```

Expected: JSON response from chalan-01 with an AI-generated reply acknowledging the incident.

- [ ] **Step 2: Check Postgres for incident**

Run: `PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d postgres -c "SELECT * FROM experiment.incidents ORDER BY created_at DESC LIMIT 1;"`

Expected: Row appears (may be empty if the chalan's prompt doesn't explicitly write to SQL — this validates the memory and gateway are working).

- [ ] **Step 3: Check chalan-01 logs**

Run: `docker compose logs chalan-01 --tail 50`

Expected: Logs show the message being received, provider call being made, and response being sent.

- [ ] **Step 4: Verify memory was stored**

Run: `PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d postgres -c "SELECT key, category, substring(content, 1, 80) FROM experiment.memories ORDER BY created_at DESC LIMIT 5;"`

Expected: Memory entries from chalan-01's interaction.

---

## Execution Notes

- **API Key required**: The ZeroClaw containers need a valid `OPENROUTER_API_KEY` in `.env`. Without it, LLM calls will fail. Get one from https://openrouter.ai.
- **First build is slow**: The Rust compilation in the ZeroClaw Dockerfile takes 5-15 minutes. Subsequent builds use Docker layer caching and are fast.
- **Postgres port**: Mapped to 5433 on host to avoid conflict with the local Postgres on 5432.
- **Mailpit**: Web UI at http://localhost:8025 shows all emails sent by chalanes. IMAP on 1143 for the email channel.
