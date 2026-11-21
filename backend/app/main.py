import asyncio
import json
import os
import urllib.request
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, Base, get_db
from app.core.auth import (
    verify_password,
    create_access_token,
    get_current_user,
    get_optional_user,
    require_roles,
)
from app.services.pgrouting_service import find_nearest_road_node, compute_pgrouting_path
from app.routers.portals import router as portals_router

app = FastAPI(title=settings.APP_NAME, version="3.0.0")
app.include_router(portals_router)

static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Atomic concurrency lock for ambulance reservation
dispatch_lock = asyncio.Lock()

# Simulated traffic state label (explicitly tracked & labeled as simulated)
TRAFFIC_SIM_STATE = {
    "mode": "NORMAL",
    "is_simulated": True,
    "multiplier": 1.0,
    "updated_at": datetime.utcnow().isoformat() + "Z",
}


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
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                self.disconnect(connection)


ws_manager = ConnectionManager()


@app.on_event("startup")
async def startup_event():
    try:
        async with engine.begin() as conn:
            if "postgresql" in settings.DATABASE_URL:
                await conn.execute(text("CREATE SCHEMA IF NOT EXISTS iers"))
    except Exception as e:
        print(f"[STARTUP] Notice: {e}")


@app.get("/")
def root():
    frontend_index = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "index.html"))
    if os.path.exists(frontend_index):
        return FileResponse(frontend_index)
    return {"status": "online", "system": settings.APP_NAME}


# ============================================================================
# QGIS TILE SERVER PROXY (Zero External Tile API Keys)
# ============================================================================
@app.get("/tiles/{z}/{x}/{y}.png")
async def proxy_qgis_tile(z: int, x: int, y: int):
    """Proxies XYZ map tiles rendered directly from Intelligent_Emergency_Response.qgz via PyQGIS."""
    url = f"http://127.0.0.1:8001/tiles/{z}/{x}/{y}.png"

    def _fetch():
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read()

    try:
        data = await asyncio.to_thread(_fetch)
        return Response(content=data, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"QGIS tile server unavailable: {e}")


# ============================================================================
# ROLE-BASED AUTHENTICATION (JWT + RBAC)
# ============================================================================
@app.post("/api/v1/auth/login")
async def login(payload: dict, db: AsyncSession = Depends(get_db)):
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    res = await db.execute(
        text("SELECT user_id, name, email, password_hash, role, assigned_entity_id, is_active FROM iers.users WHERE LOWER(email) = :email"),
        {"email": email},
    )
    user = res.mappings().first()
    if not user or not user["is_active"] or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token({
        "sub": user["user_id"],
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
        "assigned_entity_id": user["assigned_entity_id"],
    })
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "user_id": user["user_id"],
            "name": user["name"],
            "email": user["email"],
            "role": user["role"],
            "assigned_entity_id": user["assigned_entity_id"],
        },
    }


@app.get("/api/v1/auth/me")
async def get_me(user: Dict[str, Any] = Depends(get_current_user)):
    return user


@app.get("/api/v1/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("DISPATCHER")),
):
    res = await db.execute(
        text("SELECT user_id, name, email, role, assigned_entity_id, is_active, created_at FROM iers.users ORDER BY role, name")
    )
    return [dict(r) for r in res.mappings().all()]


# ============================================================================
# SYSTEM & GIS DIAGNOSTICS
# ============================================================================
@app.get("/api/v1/system/database-info")
async def get_database_info(db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("""
        SELECT
            current_database() AS db_name,
            current_user AS db_user,
            current_schema() AS active_schema,
            inet_server_port() AS server_port,
            postgis_version() AS postgis_ver,
            pgr_version() AS pgrouting_ver
    """))
    row = dict(res.fetchone()._mapping)

    counts = (await db.execute(text("""
        SELECT
            (SELECT count(*) FROM iers.road_nodes) AS road_nodes,
            (SELECT count(*) FROM iers.roads) AS road_edges,
            (SELECT count(*) FROM iers.roads WHERE is_oneway) AS oneway_edges,
            (SELECT count(*) FROM iers.roads WHERE emergency_contraflow_allowed) AS contraflow_allowed_edges,
            (SELECT count(*) FROM iers.roads WHERE is_blocked) AS blocked_edges,
            (SELECT count(*) FROM iers.roads WHERE is_emergency_corridor) AS corridor_edges,
            (SELECT count(*) FROM iers.ambulances) AS ambulances,
            (SELECT count(*) FROM iers.hospitals) AS hospitals,
            (SELECT count(*) FROM iers.incidents) AS incidents,
            (SELECT count(*) FROM iers.users) AS users,
            (SELECT round((sum(distance_m)/1000.0)::numeric, 1) FROM iers.roads) AS total_road_km
    """))).mappings().first()

    clean_url = settings.DATABASE_URL
    if "@" in clean_url:
        protocol = clean_url.split("://")[0]
        host_db = clean_url.split("@")[1]
        clean_url = f"{protocol}://***:***@{host_db}"

    return {
        "status": "connected",
        "operational_rdbms": "PostgreSQL 17 + PostGIS 3.5 + pgRouting 3.7.3",
        "sqlite_in_use": False,
        "database_url_redacted": clean_url,
        "diagnostics": row,
        "verified_counts": {k: (float(v) if hasattr(v, "__float__") and k == "total_road_km" else int(v or 0)) for k, v in dict(counts).items()},
    }


@app.get("/api/v1/gis/extent")
async def get_gis_extent(db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text("""
        SELECT
            ST_XMin(ext) AS min_lon, ST_YMin(ext) AS min_lat,
            ST_XMax(ext) AS max_lon, ST_YMax(ext) AS max_lat
        FROM (SELECT ST_Extent(geom) AS ext FROM iers.roads) s
    """))).mappings().first()
    return dict(row)


@app.get("/api/v1/gis/roads-overlay")
async def get_roads_overlay(
    filter_type: str = "arterial",
    db: AsyncSession = Depends(get_db),
):
    """
    Returns lightweight PostGIS GeoJSON overlays for interactive map layers:
    - arterial: trunk & primary highways
    - congested: roads with traffic_multiplier >= 2.0
    - blocked: closed roads (is_blocked = TRUE)
    - corridors: active emergency corridors
    - oneway: one-way roads showing emergency contraflow permission
    """
    where_map = {
        "arterial": "highway IN ('trunk', 'primary')",
        "congested": "traffic_multiplier >= 2.0 AND NOT is_blocked",
        "blocked": "is_blocked = TRUE",
        "corridors": "is_emergency_corridor = TRUE",
        "oneway": "is_oneway = TRUE",
    }
    where_clause = where_map.get(filter_type, where_map["arterial"])
    res = await db.execute(text(f"""
        SELECT road_id, name, highway, distance_m, traffic_multiplier,
               is_blocked, is_oneway, emergency_contraflow_allowed, is_emergency_corridor,
               ST_AsGeoJSON(geom)::json AS geometry
        FROM iers.roads
        WHERE geom IS NOT NULL AND {where_clause}
        LIMIT 2500
    """))
    features = []
    for r in res.mappings().all():
        features.append({
            "type": "Feature",
            "geometry": r["geometry"],
            "properties": {
                "road_id": r["road_id"],
                "name": r["name"] or f"{r['highway']} road",
                "highway": r["highway"],
                "distance_m": round(float(r["distance_m"] or 0), 1),
                "traffic_multiplier": float(r["traffic_multiplier"] or 1.0),
                "is_blocked": bool(r["is_blocked"]),
                "is_oneway": bool(r["is_oneway"]),
                "emergency_contraflow_allowed": bool(r["emergency_contraflow_allowed"]),
                "is_emergency_corridor": bool(r["is_emergency_corridor"]),
            },
        })
    return {"type": "FeatureCollection", "filter": filter_type, "count": len(features), "features": features}


# ============================================================================
# HOSPITALS & TRAUMA CENTRES (Real OSM Coordinates)
# ============================================================================
@app.get("/api/v1/hospitals")
async def get_hospitals(
    city: Optional[str] = None,
    trauma_only: bool = False,
    icu_only: bool = False,
    db: AsyncSession = Depends(get_db),
):
    clauses = ["1=1"]
    params: Dict[str, Any] = {}
    if city:
        clauses.append("LOWER(city) = LOWER(:city)")
        params["city"] = city
    if trauma_only:
        clauses.append("has_trauma_care = TRUE")
    if icu_only:
        clauses.append("icu_available > 0")

    res = await db.execute(
        text(f"""
            SELECT hospital_id AS id, name, latitude, longitude, total_beds, icu_available,
                   trauma_level, has_helipad, nearest_node_id, osm_id,
                   has_icu, has_trauma_care, has_cardiology, contact_number, city, attributes_simulated
            FROM iers.hospitals
            WHERE {' AND '.join(clauses)}
            ORDER BY trauma_level ASC, icu_available DESC, name ASC
        """),
        params,
    )
    return [dict(r) for r in res.mappings().all()]


@app.patch("/api/v1/hospitals/{hospital_id}/capacity")
async def update_hospital_capacity(
    hospital_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF", "ADMIN")),
):
    if user["role"] == "HOSPITAL_STAFF" and user.get("assigned_entity_id") and user["assigned_entity_id"] != hospital_id:
        raise HTTPException(status_code=403, detail="Hospital staff can only update their assigned facility")

    icu = int(payload.get("icu_available", 0))
    beds = int(payload.get("total_beds", 100))
    if icu < 0 or beds < 0:
        raise HTTPException(status_code=400, detail="Bed counts must be non-negative")

    res = await db.execute(
        text("""
            UPDATE iers.hospitals
            SET icu_available = :icu, total_beds = :beds
            WHERE hospital_id = :hid
            RETURNING hospital_id AS id, name, total_beds, icu_available, trauma_level
        """),
        {"icu": icu, "beds": beds, "hid": hospital_id},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Hospital not found")
    await db.commit()
    await ws_manager.broadcast({"type": "HOSPITAL_UPDATED", "hospital_id": hospital_id, "icu_available": icu})
    return dict(row)


@app.get("/api/v1/hospitals/recommend")
async def recommend_hospitals(
    lat: float,
    lon: float,
    priority: str = "P1",
    db: AsyncSession = Depends(get_db),
):
    """Ranks top 5 nearest hospitals using real PostGIS KNN + pgRouting road-network travel time."""
    nearest = await find_nearest_road_node(db, lat, lon)
    if not nearest:
        raise HTTPException(status_code=400, detail="Coordinates outside road network")
    origin_node, snap_dist = nearest

    cand_res = await db.execute(
        text("""
            SELECT hospital_id AS id, name, latitude, longitude, icu_available, total_beds,
                   trauma_level, has_trauma_care, has_cardiology, nearest_node_id, city,
                   ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) AS crow_dist_m
            FROM iers.hospitals
            WHERE nearest_node_id IS NOT NULL
            ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
            LIMIT 6
        """),
        {"lat": lat, "lon": lon},
    )
    candidates = cand_res.mappings().all()
    ranked = []
    for h in candidates:
        route = await compute_pgrouting_path(
            db, origin_node, int(h["nearest_node_id"]), algorithm="ASTAR", vehicle_mode="AMBULANCE"
        )
        eta_min = route["eta_minutes"] if route else round(float(h["crow_dist_m"]) / 500.0, 1)
        dist_km = route["total_distance_km"] if route else round(float(h["crow_dist_m"]) / 1000.0, 2)
        suitability = 100.0 - eta_min * 2.5
        if priority == "P1" and h["has_trauma_care"]:
            suitability += 20.0
        if (h["icu_available"] or 0) > 0:
            suitability += 10.0
        ranked.append({
            **dict(h),
            "network_distance_km": dist_km,
            "network_eta_minutes": eta_min,
            "suitability_score": round(max(suitability, 1.0), 1),
        })
    ranked.sort(key=lambda x: x["suitability_score"], reverse=True)
    return {"origin_node": origin_node, "snap_distance_m": round(snap_dist, 1), "recommendations": ranked}


# ============================================================================
# AMBULANCE FLEET MANAGEMENT
# ============================================================================
@app.get("/api/v1/ambulances")
async def get_ambulances(db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("""
        SELECT ambulance_id AS id, call_sign, registration_number, vehicle_type, status,
               latitude, longitude, current_node_id, height_meters, weight_tons, width_meters,
               driver_name, fuel_percent, capabilities, updated_at
        FROM iers.ambulances
        ORDER BY ambulance_id
    """))
    return [dict(r) for r in res.mappings().all()]


@app.post("/api/v1/ambulances")
async def register_ambulance(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("DISPATCHER")),
):
    call_sign = (payload.get("call_sign") or "").strip()
    if not call_sign:
        raise HTTPException(status_code=400, detail="call_sign is required")
    lat = float(payload.get("latitude", 15.3696))
    lon = float(payload.get("longitude", 75.1231))
    vtype = (payload.get("vehicle_type") or "ALS").upper()
    nearest = await find_nearest_road_node(db, lat, lon)
    node_id = str(nearest[0]) if nearest else None

    cnt = (await db.execute(text("SELECT count(*) FROM iers.ambulances"))).scalar()
    amb_id = f"AMB_{int(cnt) + 1:02d}"
    reg = payload.get("registration_number") or f"KA-25-G-{1000 + int(cnt) + 1}"
    caps = json.dumps(payload.get("capabilities") or (["ALS", "DEFIBRILLATOR", "VENTILATOR"] if vtype == "ALS" else ["BLS", "OXYGEN"]))

    await db.execute(
        text("""
            INSERT INTO iers.ambulances (
                ambulance_id, call_sign, registration_number, vehicle_type, status,
                latitude, longitude, current_node_id, geom, height_meters, weight_tons, width_meters,
                driver_name, capabilities
            ) VALUES (
                :id, :cs, :reg, :vt, 'AVAILABLE',
                :lat, :lon, :nid, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :h, :wt, :w,
                :drv, CAST(:caps AS jsonb)
            )
        """),
        {
            "id": amb_id, "cs": call_sign, "reg": reg, "vt": vtype,
            "lat": lat, "lon": lon, "nid": node_id,
            "h": float(payload.get("height_meters", 2.6 if vtype == "ALS" else 2.2)),
            "wt": float(payload.get("weight_tons", 4.2 if vtype == "ALS" else 2.8)),
            "w": float(payload.get("width_meters", 2.3 if vtype == "ALS" else 2.0)),
            "drv": payload.get("driver_name") or "Paramedic Crew",
            "caps": caps,
        },
    )
    await db.commit()
    await ws_manager.broadcast({"type": "FLEET_UPDATED", "ambulance_id": amb_id})
    return {"id": amb_id, "call_sign": call_sign, "registration_number": reg, "status": "AVAILABLE", "current_node_id": node_id}


@app.patch("/api/v1/ambulances/{ambulance_id}/status")
async def update_ambulance_status(
    ambulance_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("DISPATCHER", "AMBULANCE_OPERATOR")),
):
    new_status = (payload.get("status") or "").upper()
    if new_status not in ("AVAILABLE", "ASSIGNED", "ACCEPTED", "EN_ROUTE", "ARRIVED_AT_SCENE", "PATIENT_PICKED_UP", "EN_ROUTE_TO_HOSPITAL", "ARRIVED_AT_HOSPITAL", "COMPLETED", "UNAVAILABLE", "BUSY", "OFFLINE"):
        raise HTTPException(status_code=400, detail="Invalid ambulance status")

    if user["role"] == "AMBULANCE_OPERATOR" and user.get("assigned_entity_id") and user["assigned_entity_id"] != ambulance_id:
        raise HTTPException(status_code=403, detail="Operator can only update their assigned ambulance")

    res = await db.execute(
        text("""
            UPDATE iers.ambulances
            SET status = :st, updated_at = NOW()
            WHERE ambulance_id = :aid
            RETURNING ambulance_id AS id, call_sign, status, latitude, longitude
        """),
        {"st": new_status, "aid": ambulance_id},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Ambulance not found")
    await db.commit()
    await ws_manager.broadcast({"type": "AMBULANCE_STATUS", **dict(row)})
    return dict(row)


@app.post("/api/v1/ambulances/{ambulance_id}/move")
async def move_ambulance_simulated(
    ambulance_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("ADMIN", "DISPATCHER", "AMBULANCE_CREW")),
):
    """Simulated GPS telemetry update for prototype movement along a route."""
    lat = float(payload["latitude"])
    lon = float(payload["longitude"])
    nearest = await find_nearest_road_node(db, lat, lon)
    node_id = str(nearest[0]) if nearest else None

    await db.execute(
        text("""
            UPDATE iers.ambulances
            SET latitude = :lat, longitude = :lon, current_node_id = :nid,
                geom = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), updated_at = NOW()
            WHERE ambulance_id = :aid
        """),
        {"lat": lat, "lon": lon, "nid": node_id, "aid": ambulance_id},
    )
    await db.commit()
    await ws_manager.broadcast({"type": "AMBULANCE_MOVED", "ambulance_id": ambulance_id, "latitude": lat, "longitude": lon})
    return {"ambulance_id": ambulance_id, "latitude": lat, "longitude": lon, "current_node_id": node_id, "simulated": True}


# ============================================================================
# EMERGENCY MANAGEMENT & CONCURRENT DISPATCH
# ============================================================================
def _classify_priority(incident_type: str, people_affected: int, requested_priority: Optional[str]) -> tuple[str, int, bool]:
    """Rule-based P1-P4 priority classification with human-review flag for uncertain cases."""
    low = (incident_type or "").lower()
    if requested_priority in ("P1", "P2", "P3", "P4"):
        score_map = {"P1": 95, "P2": 75, "P3": 50, "P4": 25}
        return requested_priority, score_map[requested_priority], False

    if any(k in low for k in ("cardiac", "stroke", "unconscious", "severe trauma", "mass casualty", "fire", "hemorrhage")):
        return "P1", 95, False
    if people_affected >= 3 or any(k in low for k in ("accident", "collision", "fracture", "breathing", "chest pain")):
        return "P2", 78, False
    if any(k in low for k in ("fall", "burn", "seizure", "fever")):
        return "P3", 52, False
    # Uncertain / unclassified description -> flag for human dispatcher review
    return "P3", 45, True


@app.get("/api/v1/incidents")
async def get_incidents(db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("""
        SELECT i.incident_id AS id, i.incident_type, i.severity, i.priority, i.priority_score,
               i.people_affected, i.description, i.needs_review, i.status,
               i.latitude, i.longitude, i.nearest_node_id, i.caller_name, i.caller_phone,
               i.assigned_ambulance_id, i.target_hospital_id, i.location_source,
               i.reported_at, i.dispatched_at, i.resolved_at,
               a.call_sign AS ambulance_callsign,
               h.name AS hospital_name
        FROM iers.incidents i
        LEFT JOIN iers.ambulances a ON a.ambulance_id = i.assigned_ambulance_id
        LEFT JOIN iers.hospitals h ON h.hospital_id = i.target_hospital_id
        ORDER BY
            CASE i.priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 4 END,
            i.reported_at DESC
    """))
    return [dict(r) for r in res.mappings().all()]


@app.get("/api/v1/incidents/{incident_id}/events")
async def get_incident_events(incident_id: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        text("""
            SELECT event_id, incident_id, old_status, new_status, changed_by, remarks, created_at
            FROM iers.emergency_events
            WHERE incident_id = :iid
            ORDER BY created_at DESC
        """),
        {"iid": incident_id},
    )
    return [dict(r) for r in res.mappings().all()]


@app.post("/api/v1/incidents/create")
async def create_and_dispatch_incident(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Optional[Dict[str, Any]] = Depends(get_optional_user),
):
    """
    Registers an emergency incident, applies P1-P4 classification rules,
    atomically allocates the best available ambulance + trauma hospital using
    real pgRouting over Hubballi-Dharwad roads, and logs the event timeline.
    """
    lat = float(payload.get("latitude", 15.3647))
    lon = float(payload.get("longitude", 75.1240))
    incident_type = payload.get("incident_type", "Acute Cardiac Event")
    description = payload.get("description", "")
    people = int(payload.get("people_affected", 1))
    caller_name = payload.get("caller_name", "Emergency Caller")
    caller_phone = payload.get("caller_phone", "112")
    loc_source = payload.get("location_source", "MANUAL_PIN")
    algorithm = (payload.get("algorithm") or "ASTAR").upper()

    priority, p_score, needs_review = _classify_priority(incident_type, people, payload.get("priority"))
    severity = "CRITICAL" if priority == "P1" else ("URGENT" if priority == "P2" else "STANDARD")

    nearest = await find_nearest_road_node(db, lat, lon)
    if not nearest:
        raise HTTPException(status_code=400, detail="Incident location outside Hubballi-Dharwad road network")
    inc_node, _ = nearest
    inc_id = f"INC_{int(datetime.utcnow().timestamp() * 1000)}"
    actor = user["email"] if user else "system"

    async with dispatch_lock:
        # 1. Pick nearest available ambulance
        amb_res = await db.execute(
            text("""
                SELECT ambulance_id, call_sign, vehicle_type, latitude, longitude, current_node_id,
                       height_meters, width_meters, weight_tons, driver_name
                FROM iers.ambulances
                WHERE status = 'AVAILABLE' AND current_node_id IS NOT NULL
                ORDER BY
                    CASE WHEN :prio = 'P1' AND vehicle_type = 'ALS' THEN 0 ELSE 1 END,
                    geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                LIMIT 1
            """),
            {"prio": priority, "lat": lat, "lon": lon},
        )
        amb = amb_res.mappings().first()

        # 2. Pick best suitable hospital
        hosp_res = await db.execute(
            text("""
                SELECT hospital_id, name, latitude, longitude, nearest_node_id, icu_available, trauma_level
                FROM iers.hospitals
                WHERE nearest_node_id IS NOT NULL AND icu_available > 0
                ORDER BY
                    CASE WHEN :prio = 'P1' AND has_trauma_care THEN 0 ELSE 1 END,
                    geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                LIMIT 1
            """),
            {"prio": priority, "lat": lat, "lon": lon},
        )
        hosp = hosp_res.mappings().first()

        route_to_scene = None
        route_to_hosp = None
        status_str = "QUEUED"
        amb_id = None
        hosp_id = hosp["hospital_id"] if hosp else None

        if amb and hosp:
            route_to_scene = await compute_pgrouting_path(
                db, int(amb["current_node_id"]), inc_node,
                algorithm=algorithm, vehicle_mode="AMBULANCE",
                vehicle_height=float(amb["height_meters"] or 2.4),
                vehicle_width=float(amb["width_meters"] or 2.2),
                vehicle_weight=float(amb["weight_tons"] or 3.5),
            )
            route_to_hosp = await compute_pgrouting_path(
                db, inc_node, int(hosp["nearest_node_id"]),
                algorithm=algorithm, vehicle_mode="AMBULANCE",
                vehicle_height=float(amb["height_meters"] or 2.4),
                vehicle_width=float(amb["width_meters"] or 2.2),
                vehicle_weight=float(amb["weight_tons"] or 3.5),
            )
            if route_to_scene and route_to_hosp:
                status_str = "ALLOCATED"
                amb_id = amb["ambulance_id"]
                await db.execute(
                    text("UPDATE iers.ambulances SET status = 'ASSIGNED', updated_at = NOW() WHERE ambulance_id = :aid"),
                    {"aid": amb_id},
                )

        await db.execute(
            text("""
                INSERT INTO iers.incidents (
                    incident_id, incident_type, severity, priority, priority_score,
                    people_affected, description, needs_review, status,
                    latitude, longitude, geom, nearest_node_id,
                    caller_name, caller_phone, assigned_ambulance_id, target_hospital_id,
                    reported_by, location_source, dispatched_at
                ) VALUES (
                    :iid, :itype, :sev, :prio, :pscore,
                    :ppl, :desc, :rev, :st,
                    :lat, :lon, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :nid,
                    :cname, :cphone, :aid, :hid,
                    :actor, :ls, CASE WHEN :st = 'ALLOCATED' THEN NOW() ELSE NULL END
                )
            """),
            {
                "iid": inc_id, "itype": incident_type, "sev": severity, "prio": priority, "pscore": p_score,
                "ppl": people, "desc": description, "rev": needs_review, "st": status_str,
                "lat": lat, "lon": lon, "nid": str(inc_node),
                "cname": caller_name, "cphone": caller_phone, "aid": amb_id, "hid": hosp_id,
                "actor": actor, "ls": loc_source,
            },
        )

        await db.execute(
            text("""
                INSERT INTO iers.emergency_events (incident_id, old_status, new_status, changed_by, remarks)
                VALUES (:iid, NULL, :st, :actor, :rem)
            """),
            {
                "iid": inc_id, "st": status_str, "actor": actor,
                "rem": f"Classified {priority} ({'human review flagged' if needs_review else 'rule-verified'}). "
                       + (f"Assigned {amb['call_sign']} -> {hosp['name']}" if amb_id else "Queued awaiting available unit"),
            },
        )

        if route_to_hosp:
            await db.execute(
                text("""
                    INSERT INTO iers.route_history (
                        incident_id, ambulance_id, algorithm, vehicle_mode,
                        start_node, end_node, distance_m, estimated_time_s, contraflow_edges, route_geom
                    ) VALUES (
                        :iid, :aid, :algo, 'AMBULANCE',
                        :sn, :en, :dm, :ts, :cf, ST_SetSRID(ST_GeomFromGeoJSON(:gjson), 4326)
                    )
                """),
                {
                    "iid": inc_id, "aid": amb_id, "algo": route_to_hosp["algorithm"],
                    "sn": route_to_hosp["start_node"], "en": route_to_hosp["end_node"],
                    "dm": route_to_hosp["total_distance_meters"], "ts": route_to_hosp["total_duration_seconds"],
                    "cf": route_to_hosp["contraflow_edges_used"],
                    "gjson": json.dumps(route_to_hosp["geojson"]["geometry"]),
                },
            )

        await db.commit()

    await ws_manager.broadcast({"type": "INCIDENT_CREATED", "incident_id": inc_id, "status": status_str, "priority": priority})
    return {
        "status": "DISPATCHED" if status_str == "ALLOCATED" else "QUEUED",
        "incident_id": inc_id,
        "priority": priority,
        "needs_review": needs_review,
        "assigned_ambulance": dict(amb) if amb_id else None,
        "target_hospital": dict(hosp) if hosp else None,
        "route_to_scene": route_to_scene,
        "route_to_hospital": route_to_hosp,
    }


@app.patch("/api/v1/incidents/{incident_id}/status")
async def update_incident_status(
    incident_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("DISPATCHER", "AMBULANCE_OPERATOR")),
):
    new_status = (payload.get("status") or "").upper()
    remarks = payload.get("remarks") or ""
    if new_status not in ("QUEUED", "ALLOCATED", "EN_ROUTE", "ON_SCENE", "TRANSPORTING", "COMPLETED", "CANCELLED"):
        raise HTTPException(status_code=400, detail="Invalid incident status")

    cur_row = (await db.execute(
        text("SELECT status, assigned_ambulance_id FROM iers.incidents WHERE incident_id = :iid"),
        {"iid": incident_id},
    )).mappings().first()
    if not cur_row:
        raise HTTPException(status_code=404, detail="Incident not found")

    await db.execute(
        text("""
            UPDATE iers.incidents
            SET status = :st,
                needs_review = CASE WHEN :approve THEN FALSE ELSE needs_review END,
                resolved_at = CASE WHEN :st IN ('COMPLETED', 'CANCELLED') THEN NOW() ELSE resolved_at END
            WHERE incident_id = :iid
        """),
        {"st": new_status, "approve": bool(payload.get("approve_review", False)), "iid": incident_id},
    )

    if new_status in ("COMPLETED", "CANCELLED") and cur_row["assigned_ambulance_id"]:
        await db.execute(
            text("UPDATE iers.ambulances SET status = 'AVAILABLE', updated_at = NOW() WHERE ambulance_id = :aid"),
            {"aid": cur_row["assigned_ambulance_id"]},
        )

    await db.execute(
        text("""
            INSERT INTO iers.emergency_events (incident_id, old_status, new_status, changed_by, remarks)
            VALUES (:iid, :old, :new, :actor, :rem)
        """),
        {"iid": incident_id, "old": cur_row["status"], "new": new_status, "actor": user["email"], "rem": remarks or f"Updated by {user['role']}"},
    )
    await db.commit()
    await ws_manager.broadcast({"type": "INCIDENT_STATUS", "incident_id": incident_id, "status": new_status})
    return {"incident_id": incident_id, "old_status": cur_row["status"], "new_status": new_status}


# ============================================================================
# CONSTRAINT-AWARE ROUTING (pgRouting Dijkstra / A*, Normal vs Ambulance)
# ============================================================================
@app.post("/api/v1/routing/calculate")
async def calculate_route(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Computes real pgRouting path (DIJKSTRA or ASTAR) on iers.roads (40,394 edges).
    Accepts either node IDs (start_node/end_node) or lat/lon pairs (start_lat/start_lon, end_lat/end_lon).
    Optionally computes an alternative route for comparison.
    """
    start_node = payload.get("start_node")
    end_node = payload.get("end_node")
    if start_node is None and payload.get("start_lat") is not None:
        sn = await find_nearest_road_node(db, float(payload["start_lat"]), float(payload["start_lon"]))
        start_node = sn[0] if sn else None
    if end_node is None and payload.get("end_lat") is not None:
        en = await find_nearest_road_node(db, float(payload["end_lat"]), float(payload["end_lon"]))
        end_node = en[0] if en else None

    if start_node is None or end_node is None:
        raise HTTPException(status_code=400, detail="Provide start_node/end_node or start_lat/start_lon and end_lat/end_lon")

    algo = (payload.get("algorithm") or "DIJKSTRA").upper()
    vmode = (payload.get("vehicle_mode") or "AMBULANCE").upper()
    height = float(payload.get("vehicle_height", 2.4))
    width = float(payload.get("vehicle_width", 2.2))
    weight = float(payload.get("vehicle_weight", 3.5))
    include_alt = bool(payload.get("include_alternative", True))

    primary = await compute_pgrouting_path(
        db, int(start_node), int(end_node),
        algorithm=algo, vehicle_mode=vmode,
        vehicle_height=height, vehicle_width=width, vehicle_weight=weight,
    )
    if not primary:
        return {"success": False, "message": "No viable route found satisfying road restrictions and vehicle clearance"}

    alternative = None
    if include_alt and primary["edge_ids"]:
        alternative = await compute_pgrouting_path(
            db, int(start_node), int(end_node),
            algorithm=algo, vehicle_mode=vmode,
            vehicle_height=height, vehicle_width=width, vehicle_weight=weight,
            penalized_edges=primary["edge_ids"],
        )

    return {"success": True, "primary_route": primary, "alternative_route": alternative}


@app.post("/api/v1/routing/compare")
async def compare_normal_vs_ambulance(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Runs the SAME origin/destination pair for both NORMAL vehicle (strict one-way)
    and AMBULANCE (authorized urban one-way contraflow + emergency corridor relief).
    """
    start_node = payload.get("start_node")
    end_node = payload.get("end_node")
    if start_node is None and payload.get("start_lat") is not None:
        sn = await find_nearest_road_node(db, float(payload["start_lat"]), float(payload["start_lon"]))
        start_node = sn[0] if sn else None
    if end_node is None and payload.get("end_lat") is not None:
        en = await find_nearest_road_node(db, float(payload["end_lat"]), float(payload["end_lon"]))
        end_node = en[0] if en else None

    algo = (payload.get("algorithm") or "DIJKSTRA").upper()
    normal_route = await compute_pgrouting_path(db, int(start_node), int(end_node), algorithm=algo, vehicle_mode="NORMAL")
    ambulance_route = await compute_pgrouting_path(db, int(start_node), int(end_node), algorithm=algo, vehicle_mode="AMBULANCE")

    time_saved_s = None
    if normal_route and ambulance_route:
        time_saved_s = round(normal_route["total_duration_seconds"] - ambulance_route["total_duration_seconds"], 1)

    return {
        "algorithm": algo,
        "start_node": int(start_node),
        "end_node": int(end_node),
        "normal_vehicle": normal_route,
        "ambulance": ambulance_route,
        "time_saved_seconds": time_saved_s,
    }


@app.get("/api/v1/routing/history")
async def get_route_history(db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("""
        SELECT route_id, incident_id, ambulance_id, algorithm, vehicle_mode,
               start_node, end_node, round(distance_m::numeric, 1) AS distance_m,
               round(estimated_time_s::numeric, 1) AS estimated_time_s,
               contraflow_edges, calculated_at
        FROM iers.route_history
        ORDER BY calculated_at DESC
        LIMIT 50
    """))
    return [dict(r) for r in res.mappings().all()]


# ============================================================================
# CONTROLLABLE TRAFFIC SIMULATION (Explicitly Labeled Simulated)
# ============================================================================
@app.get("/api/v1/traffic/summary")
async def get_traffic_summary(db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text("""
        SELECT
            count(*) FILTER (WHERE traffic_multiplier <= 1.2 AND NOT is_blocked) AS free_flow_roads,
            count(*) FILTER (WHERE traffic_multiplier > 1.2 AND traffic_multiplier < 2.2 AND NOT is_blocked) AS moderate_roads,
            count(*) FILTER (WHERE traffic_multiplier >= 2.2 AND NOT is_blocked) AS congested_roads,
            count(*) FILTER (WHERE is_blocked) AS closed_roads,
            count(*) FILTER (WHERE is_emergency_corridor) AS emergency_corridors,
            round(avg(traffic_multiplier)::numeric, 2) AS avg_multiplier
        FROM iers.roads
    """))).mappings().first()
    return {
        **TRAFFIC_SIM_STATE,
        "is_simulated": True,
        "label": "SIMULATED TRAFFIC MODEL (Not live sensor feed)",
        "stats": {k: (float(v) if k == "avg_multiplier" else int(v or 0)) for k, v in dict(row).items()},
    }


@app.post("/api/v1/traffic/simulate")
async def simulate_traffic(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("DISPATCHER", "ADMIN")),
):
    """Applies simulated traffic density multipliers to Hubballi-Dharwad arterial/urban roads."""
    level = (payload.get("level") or "MODERATE").upper()
    presets = {
        "FREE_FLOW": (1.0, "highway IS NOT NULL"),
        "MODERATE": (1.6, "highway IN ('trunk', 'primary', 'secondary')"),
        "RUSH_HOUR": (2.8, "highway IN ('trunk', 'primary', 'secondary', 'tertiary')"),
        "SEVERE": (3.8, "highway IN ('trunk', 'primary', 'secondary', 'tertiary')"),
    }
    if level not in presets:
        raise HTTPException(status_code=400, detail="level must be FREE_FLOW, MODERATE, RUSH_HOUR, or SEVERE")

    mult, where_hw = presets[level]
    if level == "FREE_FLOW":
        await db.execute(text("UPDATE iers.roads SET traffic_multiplier = 1.0, traffic_level = 'normal', current_travel_time_s = GREATEST(travel_time_s, 0.5)"))
    else:
        await db.execute(
            text(f"""
                UPDATE iers.roads
                SET traffic_multiplier = :m,
                    traffic_level = :lvl,
                    current_travel_time_s = GREATEST(travel_time_s * :m, 0.5)
                WHERE {where_hw}
            """),
            {"m": mult, "lvl": level.lower()},
        )
    await db.commit()
    TRAFFIC_SIM_STATE.update({"mode": level, "multiplier": mult, "updated_at": datetime.utcnow().isoformat() + "Z"})
    await ws_manager.broadcast({"type": "TRAFFIC_SIMULATED", "level": level, "multiplier": mult})
    return await get_traffic_summary(db)


@app.post("/api/v1/traffic/road-closure")
async def toggle_road_closure(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("DISPATCHER", "ADMIN")),
):
    """Closes or reopens a road segment by road_id or nearest coordinates."""
    blocked = bool(payload.get("is_blocked", True))
    road_id = payload.get("road_id")
    if road_id is None and payload.get("latitude") is not None:
        r = (await db.execute(
            text("SELECT road_id FROM iers.roads ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) LIMIT 1"),
            {"lat": float(payload["latitude"]), "lon": float(payload["longitude"])},
        )).first()
        road_id = r[0] if r else None
    if road_id is None:
        raise HTTPException(status_code=400, detail="Provide road_id or latitude/longitude")

    row = (await db.execute(
        text("UPDATE iers.roads SET is_blocked = :b WHERE road_id = :rid RETURNING road_id, name, highway, is_blocked"),
        {"b": blocked, "rid": int(road_id)},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Road segment not found")
    await db.commit()
    await ws_manager.broadcast({"type": "ROAD_CLOSURE", **dict(row)})
    return dict(row)


@app.post("/api/v1/traffic/toggle-emergency-corridor")
async def toggle_emergency_corridor(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("DISPATCHER", "ADMIN")),
):
    active = bool(payload.get("active", True))
    edge_ids = payload.get("edge_ids")
    if edge_ids:
        await db.execute(
            text("UPDATE iers.roads SET is_emergency_corridor = :a WHERE road_id = ANY(:ids)"),
            {"a": active, "ids": [int(x) for x in edge_ids]},
        )
    else:
        await db.execute(
            text("UPDATE iers.roads SET is_emergency_corridor = :a WHERE highway IN ('trunk', 'primary')"),
            {"a": active},
        )
    await db.commit()
    await ws_manager.broadcast({"type": "CORRIDOR_UPDATED", "active": active})
    return {"status": "updated", "emergency_corridor_active": active}


@app.post("/api/v1/traffic/reset")
async def reset_traffic(
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("DISPATCHER", "ADMIN")),
):
    await db.execute(text("""
        UPDATE iers.roads
        SET is_blocked = FALSE, is_emergency_corridor = FALSE,
            traffic_multiplier = 1.0, traffic_level = 'normal',
            current_travel_time_s = GREATEST(travel_time_s, 0.5)
    """))
    await db.commit()
    TRAFFIC_SIM_STATE.update({"mode": "FREE_FLOW", "multiplier": 1.0, "updated_at": datetime.utcnow().isoformat() + "Z"})
    await ws_manager.broadcast({"type": "TRAFFIC_RESET"})
    return await get_traffic_summary(db)


# ============================================================================
# LIVE ANALYTICS (100% Real PostGIS Queries)
# ============================================================================
@app.get("/api/v1/analytics/summary")
async def get_analytics_summary(db: AsyncSession = Depends(get_db)):
    hw = (await db.execute(text("""
        SELECT highway, count(*) AS edges, round((sum(distance_m)/1000.0)::numeric, 1) AS km
        FROM iers.roads
        GROUP BY highway
        ORDER BY edges DESC
        LIMIT 8
    """))).mappings().all()

    city_split = (await db.execute(text("""
        SELECT
            count(*) FILTER (WHERE ST_Y(ST_Centroid(geom)) < 15.40) AS hubballi_edges,
            count(*) FILTER (WHERE ST_Y(ST_Centroid(geom)) > 15.42) AS dharwad_edges,
            count(*) FILTER (WHERE ST_Y(ST_Centroid(geom)) BETWEEN 15.40 AND 15.42) AS corridor_edges
        FROM iers.roads
    """))).mappings().first()

    fleet = (await db.execute(text("""
        SELECT status, count(*) AS cnt FROM iers.ambulances GROUP BY status
    """))).mappings().all()

    hosp_tot = (await db.execute(text("""
        SELECT count(*) AS hospital_count,
               coalesce(sum(total_beds), 0) AS total_beds,
               coalesce(sum(icu_available), 0) AS icu_available,
               count(*) FILTER (WHERE has_trauma_care) AS trauma_centers
        FROM iers.hospitals
    """))).mappings().first()

    inc_tot = (await db.execute(text("""
        SELECT count(*) AS total_incidents,
               count(*) FILTER (WHERE status NOT IN ('COMPLETED', 'CANCELLED')) AS active_incidents,
               count(*) FILTER (WHERE priority = 'P1' AND status NOT IN ('COMPLETED', 'CANCELLED')) AS active_p1,
               count(*) FILTER (WHERE needs_review) AS pending_review
        FROM iers.incidents
    """))).mappings().first()

    return {
        "road_classes": [{**dict(r), "km": float(r["km"] or 0)} for r in hw],
        "regional_split": {k: int(v or 0) for k, v in dict(city_split).items()},
        "fleet_by_status": {r["status"]: int(r["cnt"]) for r in fleet},
        "hospitals": {k: int(v or 0) for k, v in dict(hosp_tot).items()},
        "incidents": {k: int(v or 0) for k, v in dict(inc_tot).items()},
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
