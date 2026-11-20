import os
import asyncio
import json
from typing import List
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

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
    # Initialize database tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Seed baseline hospital and ambulance assets if empty
    async with engine.connect() as conn:
        pass
    
    # Background simulation ticker
    asyncio.create_task(simulation_background_worker())

async def simulation_background_worker():
    """Continuously streams updates on ambulance locations and traffic dynamics."""
    while True:
        try:
            await asyncio.sleep(settings.SIMULATION_TICK_SECONDS)
            # Broadcast live state to UI
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

@app.get("/api/v1/network")
def get_network():
    return {
        "nodes": list(routing_engine.nodes_data.values()),
        "edges": list(routing_engine.edges_data.values())
    }

@app.get("/api/v1/hospitals")
async def get_hospitals(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Hospital))
    items = result.scalars().all()
    if not items:
        # Hospitals snapped directly to giant connected component of real GIS network
        h1 = Hospital(id="H_01", name="KIMS Medical Center & Trauma Hub", latitude=15.369641, longitude=75.123161, nearest_node_id="NODE_473", total_beds=180, icu_available=22, trauma_level=1, has_helipad=True)
        h2 = Hospital(id="H_02", name="Vidyanagar Emergency Care", latitude=15.365116, longitude=75.123812, nearest_node_id="NODE_964", total_beds=90, icu_available=8, trauma_level=2, has_helipad=False)
        db.add_all([h1, h2])
        await db.commit()
        return [h1, h2]
    return items

@app.get("/api/v1/ambulances")
async def get_ambulances(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Ambulance))
    items = result.scalars().all()
    if not items:
        # Fleet positioned on major intersections of connected corridor
        a1 = Ambulance(id="AMB_01", call_sign="PARAMEDIC-ALS-01", vehicle_type=VehicleType.ALS, status=AmbulanceStatus.AVAILABLE, height_meters=3.2, weight_tons=5.2, width_meters=2.6, latitude=15.368637, longitude=75.121066, current_node_id="NODE_567", driver_name="Harris Whitaker (EMT-P)")
        a2 = Ambulance(id="AMB_02", call_sign="RAPID-FLYER-02", vehicle_type=VehicleType.RAPID_FLYER, status=AmbulanceStatus.AVAILABLE, height_meters=1.8, weight_tons=0.4, width_meters=1.1, latitude=15.365116, longitude=75.123812, current_node_id="NODE_964", driver_name="Sofia Lind")
        a3 = Ambulance(id="AMB_03", call_sign="RESCUE-BLS-03", vehicle_type=VehicleType.BLS, status=AmbulanceStatus.AVAILABLE, height_meters=2.5, weight_tons=3.6, width_meters=2.3, latitude=15.369641, longitude=75.123161, current_node_id="NODE_473", driver_name="Lars Nyström")
        db.add_all([a1, a2, a3])
        await db.commit()
        return [a1, a2, a3]
    return items

@app.get("/api/v1/incidents")
async def get_incidents(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Incident))
    return result.scalars().all()

@app.post("/api/v1/incidents/create")
async def create_and_dispatch_incident(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Creates incident and performs atomic multi-criteria allocation:
    - Finds nearest capable ambulance
    - Selects target hospital with ICU capacity
    - Computes route respecting road clearance constraints
    """
    lat = float(payload.get("latitude", 59.3250))
    lon = float(payload.get("longitude", 18.0710))
    severity_str = payload.get("severity", "CRITICAL")
    severity = IncidentSeverity(severity_str)
    caller = payload.get("caller_name", "Emergency Hotline")
    incident_type = payload.get("incident_type", "Acute Cardiac Event")

    nearest_node = routing_engine.find_nearest_node(lat, lon)
    if not nearest_node:
        raise HTTPException(status_code=400, detail="Incident location outside road network domain")

    # Fetch available fleet & hospitals
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
            "incident": {
                "id": new_incident.id,
                "severity": new_incident.severity,
                "caller": new_incident.caller_name,
                "type": new_incident.incident_type
            },
            "assigned_ambulance": {
                "id": amb.id,
                "call_sign": amb.call_sign,
                "type": amb.vehicle_type
            },
            "target_hospital": {
                "id": hosp.id,
                "name": hosp.name,
                "icu_available": hosp.icu_available
            },
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
def calculate_route(payload: dict):
    start = payload.get("start_node")
    end = payload.get("end_node")
    height = float(payload.get("vehicle_height", 2.4))
    weight = float(payload.get("vehicle_weight", 3.5))
    width = float(payload.get("vehicle_width", 2.2))

    route = routing_engine.find_constrained_route(start, end, height, weight, width)
    if not route:
        return {"success": False, "message": "No physically viable route found satisfying road constraints"}
    return {"success": True, "route": route}

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

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
