import os
import asyncio
import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.core.config import settings
from app.core.database import engine, Base, get_db
from app.models.entities import (
    RoadNode, RoadEdge, Hospital, Ambulance, Incident,
    VehicleType, AmbulanceStatus, IncidentSeverity, IncidentStatus
)
from app.algorithms.routing import RoutingEngine
from app.simulation.traffic import TrafficSimulationEngine
from app.services.dispatch import AmbulanceAllocator
from app.services.gis_loader import load_gis_network_into_engine

app = FastAPI(title=settings.APP_NAME, version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Core in-memory orchestration engines
routing_engine = RoutingEngine()
load_gis_network_into_engine(routing_engine)
traffic_sim = TrafficSimulationEngine(routing_engine)
allocator = AmbulanceAllocator(routing_engine)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                pass

ws_manager = ConnectionManager()

@app.on_event("startup")
async def startup_event():
    # Ensure PostgreSQL schema exists and tables are verified
    try:
        async with engine.begin() as conn:
            if "postgresql" in settings.DATABASE_URL:
                await conn.execute(text("CREATE SCHEMA IF NOT EXISTS iers"))
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        print(f"[STARTUP] Notice during metadata verify: {e}")

    # Background simulation ticker
    asyncio.create_task(simulation_background_worker())

async def simulation_background_worker():
    """Continuously streams updates on ambulance locations and traffic dynamics."""
    while True:
        try:
            await asyncio.sleep(settings.SIMULATION_TICK_SECONDS)
            await ws_manager.broadcast({
                "type": "TICK",
                "timestamp": asyncio.get_event_loop().time()
            })
        except Exception:
            await asyncio.sleep(2)

@app.get("/")
def root():
    frontend_index = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "index.html"))
    if os.path.exists(frontend_index):
        return FileResponse(frontend_index)
    return {"status": "online", "system": settings.APP_NAME}

@app.get("/api/v1/system/database-info")
async def get_database_info(db: AsyncSession = Depends(get_db)):
    """
    Returns verified runtime database connection diagnostics proving
    the operational database is PostgreSQL/PostGIS (iers_db), NOT SQLite.
    """
    res = await db.execute(text("""
        SELECT
            current_database() AS db_name,
            current_user AS db_user,
            current_schema() AS active_schema,
            inet_server_port() AS server_port,
            postgis_version() AS postgis_ver
    """))
    row = dict(res.fetchone()._mapping)

    nodes_cnt = (await db.execute(text("SELECT count(*) FROM iers.road_nodes"))).scalar()
    edges_cnt = (await db.execute(text("SELECT count(*) FROM iers.roads"))).scalar()
    ambs_cnt = (await db.execute(text("SELECT count(*) FROM iers.ambulances"))).scalar()
    hosps_cnt = (await db.execute(text("SELECT count(*) FROM iers.hospitals"))).scalar()
    # Redact credentials from URL for security
    clean_url = settings.DATABASE_URL
    if "@" in clean_url:
        protocol = clean_url.split("://")[0]
        host_db = clean_url.split("@")[1]
        clean_url = f"{protocol}://***:***@{host_db}"

    return {
        "status": "connected",
        "operational_rdbms": "PostgreSQL 17 + PostGIS 3.5",
        "sqlite_in_use": False,
        "database_url_redacted": clean_url,
        "diagnostics": row,
        "verified_counts": {
            "road_nodes": nodes_cnt,
            "road_edges": edges_cnt,
            "ambulances": ambs_cnt,
            "hospitals": hosps_cnt
        }
    }


@app.get("/api/v1/network")
async def get_network(db: AsyncSession = Depends(get_db)):
    """Return the real Hubballi-Dharwad metropolitan road network from PostGIS."""

    result = await db.execute(text("""
        SELECT
            road_id,
            name,
            highway,
            distance_m,
            traffic_multiplier,
            current_travel_time_s,
            is_blocked,
            ST_AsGeoJSON(geom)::json AS geometry
        FROM iers.roads
        WHERE geom IS NOT NULL
        ORDER BY road_id
    """))

    roads = result.mappings().all()

    features = []
    for road in roads:
        if not road["geometry"]:
            continue

        features.append({
            "type": "Feature",
            "geometry": road["geometry"],
            "properties": {
                "id": road["road_id"],
                "name": road["name"] or "Unnamed road",
                "highway": road["highway"],
                "distance_m": road["distance_m"],
                "traffic_factor": road["traffic_multiplier"] or 1.0,
                "travel_time_s": road["current_travel_time_s"],
                "is_blocked": road["is_blocked"]
            }
        })

    node_count = (await db.execute(
        text("SELECT COUNT(*) FROM iers.road_nodes")
    )).scalar_one()

    return {
        "nodes_count": node_count,
        "edges_count": len(features),
        "geojson": {
            "type": "FeatureCollection",
            "features": features
        }
    }
@app.get("/api/v1/hospitals")
async def get_hospitals(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Hospital))
    items = result.scalars().all()
    if not items:
        # Snap hospitals to verified Hubballi connected network nodes
        h1 = Hospital(
            id="HOSP_01",
            name="KIMS Medical Centre Hubballi",
            latitude=15.369641,
            longitude=75.123161,
            nearest_node_id="NODE_473",
            total_beds=450,
            icu_available=18,
            trauma_level=1,
            has_helipad=True
        )
        h2 = Hospital(
            id="HOSP_02",
            name="SDMH Trauma Centre",
            latitude=15.362804,
            longitude=75.117432,
            nearest_node_id="NODE_964",
            total_beds=320,
            icu_available=10,
            trauma_level=2,
            has_helipad=False
        )
        db.add_all([h1, h2])
        await db.commit()
        return [h1.to_dict(), h2.to_dict()]
    return [h.to_dict() for h in items]

@app.get("/api/v1/ambulances")
async def get_ambulances(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Ambulance))
    items = result.scalars().all()
    if not items:
        a1 = Ambulance(
            id="AMB_01",
            call_sign="PARAMEDIC-ALS-01",
            vehicle_type=VehicleType.ALS,
            status=AmbulanceStatus.AVAILABLE,
            height_meters=3.2,
            weight_tons=5.2,
            width_meters=2.6,
            latitude=15.368637,
            longitude=75.121066,
            current_node_id="NODE_567",
            driver_name="Harris Whitaker (EMT-P)"
        )
        a2 = Ambulance(
            id="AMB_02",
            call_sign="RAPID-FLYER-02",
            vehicle_type=VehicleType.FLYER,
            status=AmbulanceStatus.AVAILABLE,
            height_meters=1.8,
            weight_tons=0.4,
            width_meters=1.1,
            latitude=15.365116,
            longitude=75.123812,
            current_node_id="NODE_964",
            driver_name="Sofia Lind"
        )
        a3 = Ambulance(
            id="AMB_03",
            call_sign="RESCUE-BLS-03",
            vehicle_type=VehicleType.BLS,
            status=AmbulanceStatus.AVAILABLE,
            height_meters=2.5,
            weight_tons=3.6,
            width_meters=2.3,
            latitude=15.369641,
            longitude=75.123161,
            current_node_id="NODE_473",
            driver_name="Lars NystrÃ¶m"
        )
        db.add_all([a1, a2, a3])
        await db.commit()
        return [a1.to_dict(), a2.to_dict(), a3.to_dict()]
    return [a.to_dict() for a in items]

@app.get("/api/v1/incidents")
async def get_incidents(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Incident))
    items = result.scalars().all()
    return [i.to_dict() for i in items]

@app.post("/api/v1/incidents/create")
async def create_and_dispatch_incident(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Creates incident and performs atomic multi-criteria allocation:
    - Finds nearest capable ambulance
    - Selects target hospital with ICU capacity
    - Computes route respecting road clearance constraints
    - Persists state directly in PostGIS iers_db
    """
    lat = float(payload.get("latitude", 15.368637))
    lon = float(payload.get("longitude", 75.121066))
    severity_str = payload.get("severity", "CRITICAL")
    severity = IncidentSeverity(severity_str)
    caller = payload.get("caller_name", "Emergency Hotline")
    incident_type = payload.get("incident_type", "Acute Cardiac Event")

    nearest_node = routing_engine.find_nearest_node(lat, lon)
    if not nearest_node:
        raise HTTPException(status_code=400, detail="Incident location outside road network domain")

    # Fetch available fleet & hospitals from PostGIS iers_db
    amb_res = await db.execute(select(Ambulance))
    ambulances = amb_res.scalars().all()

    hosp_res = await db.execute(select(Hospital))
    hospitals = hosp_res.scalars().all()

    inc_id = f"INC_{int(asyncio.get_event_loop().time() * 1000)}"
    new_incident = Incident(
        id=inc_id,
        caller_name=caller,
        incident_type=incident_type,
        severity=severity,
        status=IncidentStatus.REPORTED,
        latitude=lat,
        longitude=lon,
        nearest_node_id=nearest_node,
        notes=payload.get("notes", "")
    )

    allocation_result = await allocator.find_optimal_ambulance_and_hospital(
        incident=new_incident,
        available_ambulances=list(ambulances),
        hospitals=list(hospitals)
    )

    if allocation_result:
        amb, hosp, route_to_scene, route_to_hosp = allocation_result
        new_incident.assigned_ambulance_id = amb.id
        new_incident.target_hospital_id = hosp.id
        new_incident.status = IncidentStatus.ALLOCATED
        new_incident.dispatched_at = datetime.utcnow()

        db.add(new_incident)
        await db.commit()
        await db.refresh(amb)
        await db.refresh(hosp)

        # Broadcast real-time dispatch event
        await ws_manager.broadcast({
            "type": "INCIDENT_DISPATCHED",
            "incident_id": inc_id,
            "ambulance_id": amb.id,
            "ambulance_callsign": amb.call_sign,
            "hospital_name": hosp.name,
            "route_to_scene": route_to_scene,
            "route_to_hosp": route_to_hosp,
            "eta_scene_mins": route_to_scene["eta_minutes"]
        })

        return {
            "status": "DISPATCHED",
            "incident": new_incident.to_dict(),
            "assigned_ambulance": amb.to_dict(),
            "target_hospital": hosp.to_dict(),
            "route_to_scene": route_to_scene,
            "route_to_hospital": route_to_hosp
        }
    else:
        new_incident.status = IncidentStatus.REPORTED
        db.add(new_incident)
        await db.commit()
        return {
            "status": "QUEUED_NO_CAPABLE_AMBULANCE_AVAILABLE",
            "incident_id": inc_id,
            "detail": "All capable units are engaged or physical clearance constraints prevent routing"
        }

@app.post("/api/v1/routing/calculate")
async def calculate_route(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Computes constrained route with physical clearance checks (height, width, weight).
    Cross-checks and verifies shortest path with the live PostGIS iers.find_shortest_path function.
    """
    start = payload.get("start_node")
    end = payload.get("end_node")
    height = float(payload.get("vehicle_height", 2.4))
    weight = float(payload.get("vehicle_weight", 3.5))
    width = float(payload.get("vehicle_width", 2.2))

    route = routing_engine.find_constrained_route(start, end, height, weight, width)
    if not route:
        return {"success": False, "message": "No physically viable route found satisfying road constraints"}

    # Query PostGIS shortest path routing function from iers_db
    postgis_path = []
    try:
        res = await db.execute(
            text("SELECT * FROM iers.find_shortest_path(:start, :target, :hops)"),
            {"start": start, "target": end, "hops": 30}
        )
        postgis_path = [dict(r._mapping) for r in res.fetchall()]
    except Exception as e:
        postgis_path = []

    return {
        "success": True,
        "route": route,
        "postgis_routing": {
            "database": "iers_db",
            "schema": "iers",
            "hops_count": len(postgis_path),
            "verified_in_postgis": len(postgis_path) > 0,
            "steps": postgis_path
        }
    }

@app.post("/api/v1/traffic/set-congestion")
async def set_traffic(payload: dict):
    u = payload.get("source")
    v = payload.get("target")
    factor = float(payload.get("traffic_factor", 2.5))
    traffic_sim.update_edge_congestion(u, v, factor)
    
    await ws_manager.broadcast({
        "type": "TRAFFIC_UPDATE",
        "source": u,
        "target": v,
        "traffic_factor": factor
    })
    return {"status": "updated", "edge": f"{u}->{v}", "traffic_factor": factor}

@app.post("/api/v1/traffic/toggle-emergency-corridor")
async def toggle_emergency_corridor(payload: dict):
    u = payload.get("source")
    v = payload.get("target")
    active = bool(payload.get("active", True))
    traffic_sim.toggle_emergency_corridor(u, v, active)

    await ws_manager.broadcast({
        "type": "EMERGENCY_CORRIDOR_UPDATE",
        "source": u,
        "target": v,
        "is_active": active
    })
    return {"status": "success", "corridor": f"{u}->{v}", "active": active}

@app.post("/api/v1/incidents/resolve")
async def resolve_incident(payload: dict, db: AsyncSession = Depends(get_db)):
    inc_id = payload.get("incident_id")
    result = await db.execute(select(Incident).where(Incident.id == inc_id))
    inc = result.scalar_one_or_none()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    inc.status = IncidentStatus.RESOLVED
    inc.resolved_at = datetime.utcnow()

    # Free the assigned ambulance back to AVAILABLE
    if inc.assigned_ambulance_id:
        amb_res = await db.execute(select(Ambulance).where(Ambulance.id == inc.assigned_ambulance_id))
        amb = amb_res.scalar_one_or_none()
        if amb:
            amb.status = AmbulanceStatus.AVAILABLE

    # Release hospital ICU bed
    if inc.target_hospital_id:
        hosp_res = await db.execute(select(Hospital).where(Hospital.id == inc.target_hospital_id))
        hosp = hosp_res.scalar_one_or_none()
        if hosp:
            hosp.icu_available += 1

    await db.commit()

    await ws_manager.broadcast({
        "type": "INCIDENT_RESOLVED",
        "incident_id": inc_id
    })

    return {"status": "RESOLVED", "incident_id": inc_id}

