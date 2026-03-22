"""Shipment tracking mock API with simulation mode."""

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Chalanes Mock Tracking API")

SHIPMENTS: dict[str, dict] = {}
ROUTES: dict[str, list[str]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_shipments() -> None:
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
        for _ in range(3):
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
                "eta": (datetime.now(timezone.utc) + timedelta(hours=eta_hours)).isoformat(),
                "last_gps_update": _now(),
                "latitude": 19.4326 + random.uniform(-2, 2),
                "longitude": -99.1332 + random.uniform(-2, 2),
                "cargo_type": random.choice(["electronics", "food_perishable", "industrial", "retail"]),
                "weight_kg": random.randint(500, 15000),
                "history": [{"timestamp": _now(), "event": "departed", "location": origin}],
            }
            SHIPMENTS[sid] = shipment
            ROUTES[route_code].append(sid)


_seed_shipments()


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
        raise HTTPException(404, "Shipment not found")
    return SHIPMENTS[shipment_id]


@app.get("/shipments/{shipment_id}/history")
def get_history(shipment_id: str):
    if shipment_id not in SHIPMENTS:
        raise HTTPException(404, "Shipment not found")
    return {"shipment_id": shipment_id, "history": SHIPMENTS[shipment_id]["history"]}


@app.patch("/shipments/{shipment_id}")
def update_shipment(shipment_id: str, update: dict):
    if shipment_id not in SHIPMENTS:
        raise HTTPException(404, "Shipment not found")
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
        raise HTTPException(404, "Shipment not found")
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
    events = []
    for sid, s in SHIPMENTS.items():
        if s["status"] != "in_transit":
            continue

        roll = random.random()

        if roll < 0.05:
            new_eta = (datetime.fromisoformat(s["eta"]) + timedelta(hours=2)).isoformat()
            s["eta"] = new_eta
            s["history"].append({"timestamp": _now(), "event": "delayed", "new_eta": new_eta})
            events.append({"shipment": sid, "event": "delayed"})
        elif roll < 0.08:
            s["last_gps_update"] = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
            events.append({"shipment": sid, "event": "gps_dropout"})
        elif roll < 0.10:
            s["latitude"] += random.uniform(-0.5, 0.5)
            s["longitude"] += random.uniform(-0.5, 0.5)
            s["history"].append({"timestamp": _now(), "event": "route_deviation"})
            events.append({"shipment": sid, "event": "route_deviation"})
        elif roll < 0.20:
            s["status"] = "delivered"
            s["history"].append({"timestamp": _now(), "event": "delivered", "location": s["destination"]})
            events.append({"shipment": sid, "event": "delivered"})
        else:
            s["last_gps_update"] = _now()
            s["latitude"] += random.uniform(-0.01, 0.01)
            s["longitude"] += random.uniform(-0.01, 0.01)

    return {"tick": _now(), "events": events, "total_events": len(events)}
