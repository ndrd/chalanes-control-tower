-- Chalanes Control Tower — experiment tenant schema

CREATE SCHEMA IF NOT EXISTS experiment;

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

CREATE TABLE experiment.audit_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chalan_id   UUID REFERENCES experiment.chalanes(id),
    event_type  TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}',
    tokens_used INTEGER,
    cost_usd    NUMERIC(10,6),
    created_at  TIMESTAMPTZ DEFAULT now()
);

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
