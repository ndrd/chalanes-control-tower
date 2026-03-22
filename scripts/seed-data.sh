#!/usr/bin/env bash
# Seed experiment tenant with routes and drivers
set -euo pipefail

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGPASSWORD="${PGPASSWORD:-postgres}"
PGDATABASE="${PGDATABASE:-postgres}"

export PGPASSWORD

psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" <<'SQL'
INSERT INTO experiment.chalanes (name, archetype, role) VALUES
  ('coordinator', 'fleet-coordinator', 'coordinator'),
  ('chalan-01', 'control-tower', 'worker'),
  ('chalan-02', 'control-tower', 'worker')
ON CONFLICT (name) DO NOTHING;

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
