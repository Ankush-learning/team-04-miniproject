"""
Role-Scoped Stakeholder Portal Routers:
- CITIZEN: report, track own emergency, view own reports
- HOSPITAL_STAFF: manage own hospital capacity, incoming ambulances, history, CSV bulk import
- AMBULANCE_OPERATOR: current assignment, 10-state transition enforcement, GPS/simulated location, history
- DISPATCHER: severity override, bbox zoom-dependent GIS roads, data storage transparency
"""
import json
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.database import get_db
from app.core.auth import require_roles
from app.services.pgrouting_service import find_nearest_road_node, compute_pgrouting_path
from app.services.csv_import_service import (
    get_sample_csv,
    parse_and_validate_csv,
    execute_hospital_csv_import,
)

router = APIRouter(prefix="/api/v1", tags=["Stakeholder Portals"])

# Valid 10-state ambulance operator workflow
OPERATOR_STATES = (
    "AVAILABLE",
    "ASSIGNED",
    "ACCEPTED",
    "EN_ROUTE",
    "ARRIVED_AT_SCENE",
    "PATIENT_PICKED_UP",
    "EN_ROUTE_TO_HOSPITAL",
    "ARRIVED_AT_HOSPITAL",
    "COMPLETED",
    "UNAVAILABLE",
)

VALID_TRANSITIONS = {
    "AVAILABLE": {"ASSIGNED", "UNAVAILABLE"},
    "UNAVAILABLE": {"AVAILABLE"},
    "ASSIGNED": {"ACCEPTED", "UNAVAILABLE"},
    "ACCEPTED": {"EN_ROUTE"},
    "EN_ROUTE": {"ARRIVED_AT_SCENE"},
    "ARRIVED_AT_SCENE": {"PATIENT_PICKED_UP"},
    "PATIENT_PICKED_UP": {"EN_ROUTE_TO_HOSPITAL"},
    "EN_ROUTE_TO_HOSPITAL": {"ARRIVED_AT_HOSPITAL"},
    "ARRIVED_AT_HOSPITAL": {"COMPLETED"},
    "COMPLETED": {"AVAILABLE", "UNAVAILABLE"},
}


# ============================================================================
# 1. CITIZEN PORTAL (Strictly scoped to own reports)
# ============================================================================
@router.get("/citizen/my-reports")
async def citizen_my_reports(
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("CITIZEN")),
):
    res = await db.execute(
        text("""
            SELECT i.incident_id AS id, i.incident_type, i.priority, i.severity, i.status,
                   i.people_affected, i.description, i.latitude, i.longitude,
                   i.reported_at, i.dispatched_at, i.resolved_at,
                   a.call_sign AS ambulance_callsign, a.status AS ambulance_status,
                   h.name AS hospital_name
            FROM iers.incidents i
            LEFT JOIN iers.ambulances a ON a.ambulance_id = i.assigned_ambulance_id
            LEFT JOIN iers.hospitals h ON h.hospital_id = i.target_hospital_id
            WHERE i.reported_by = :email
            ORDER BY i.reported_at DESC
        """),
        {"email": user["email"]},
    )
    return [dict(r) for r in res.mappings().all()]


@router.get("/citizen/track/{incident_id}")
async def citizen_track_emergency(
    incident_id: str,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("CITIZEN")),
):
    """Returns ONLY the citizen's own incident + its assigned ambulance position & live route."""
    inc = (await db.execute(
        text("""
            SELECT i.incident_id AS id, i.incident_type, i.priority, i.status, i.latitude, i.longitude,
                   i.nearest_node_id, i.reported_at, i.assigned_ambulance_id, i.target_hospital_id,
                   a.call_sign AS ambulance_callsign, a.vehicle_type, a.status AS ambulance_status,
                   a.latitude AS amb_lat, a.longitude AS amb_lon, a.current_node_id AS amb_node,
                   a.current_speed_kmh, a.updated_at AS amb_updated_at,
                   h.name AS hospital_name, h.latitude AS hosp_lat, h.longitude AS hosp_lon
            FROM iers.incidents i
            LEFT JOIN iers.ambulances a ON a.ambulance_id = i.assigned_ambulance_id
            LEFT JOIN iers.hospitals h ON h.hospital_id = i.target_hospital_id
            WHERE i.incident_id = :iid AND i.reported_by = :email
        """),
        {"iid": incident_id, "email": user["email"]},
    )).mappings().first()

    if not inc:
        raise HTTPException(status_code=404, detail="Emergency report not found for your account")

    route = None
    if inc["amb_node"] and inc["nearest_node_id"] and inc["status"] not in ("COMPLETED", "CANCELLED"):
        route = await compute_pgrouting_path(
            db, int(inc["amb_node"]), int(inc["nearest_node_id"]), algorithm="ASTAR", vehicle_mode="AMBULANCE"
        )

    events = (await db.execute(
        text("SELECT new_status, remarks, created_at FROM iers.emergency_events WHERE incident_id = :iid ORDER BY created_at DESC"),
        {"iid": incident_id},
    )).mappings().all()

    return {"incident": dict(inc), "live_route": route, "timeline": [dict(e) for e in events]}


# ============================================================================
# 2. HOSPITAL STAFF PORTAL (Strictly scoped to user's hospital_id)
# ============================================================================
def _get_staff_hospital_id(user: Dict[str, Any]) -> str:
    hid = user.get("assigned_entity_id")
    if not hid:
        raise HTTPException(status_code=403, detail="No hospital_id associated with this account")
    return hid


@router.get("/hospital/my-facility")
async def hospital_my_facility(
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF")),
):
    hid = _get_staff_hospital_id(user)
    row = (await db.execute(
        text("""
            SELECT hospital_id AS id, name, latitude, longitude, city, total_beds, icu_available,
                   trauma_beds_available, emergency_dept_capacity, trauma_level, has_helipad,
                   has_icu, has_trauma_care, has_cardiology, contact_number,
                   service_status, service_notes, attributes_simulated
            FROM iers.hospitals
            WHERE hospital_id = :hid
        """),
        {"hid": hid},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Assigned hospital not found")
    return dict(row)


@router.patch("/hospital/my-facility")
async def hospital_update_my_facility(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF")),
):
    hid = _get_staff_hospital_id(user)
    total_beds = int(payload.get("total_beds", 0))
    icu_avail = int(payload.get("icu_available", 0))
    trauma_beds = int(payload.get("trauma_beds_available", 0))
    ed_cap = int(payload.get("emergency_dept_capacity", 0))
    if any(x < 0 for x in (total_beds, icu_avail, trauma_beds, ed_cap)):
        raise HTTPException(status_code=400, detail="Capacity values must be non-negative")
    if icu_avail > total_beds or trauma_beds > total_beds:
        raise HTTPException(status_code=400, detail="ICU/Trauma beds cannot exceed total_beds")

    svc_status = (payload.get("service_status") or "OPERATIONAL").upper()
    svc_notes = (payload.get("service_notes") or "").strip()

    await db.execute(
        text("""
            UPDATE iers.hospitals
            SET total_beds = :tb, icu_available = :icu, trauma_beds_available = :trb,
                emergency_dept_capacity = :edc,
                has_icu = COALESCE(:hicu, has_icu),
                has_trauma_care = COALESCE(:htr, has_trauma_care),
                has_cardiology = COALESCE(:hcar, has_cardiology),
                service_status = :sst, service_notes = :snt
            WHERE hospital_id = :hid
        """),
        {
            "tb": total_beds, "icu": icu_avail, "trb": trauma_beds, "edc": ed_cap,
            "hicu": payload.get("has_icu"), "htr": payload.get("has_trauma_care"),
            "hcar": payload.get("has_cardiology"), "sst": svc_status, "snt": svc_notes,
            "hid": hid,
        },
    )
    await db.execute(
        text("""
            INSERT INTO iers.hospital_capacity_history (
                hospital_id, changed_by, total_beds, icu_available,
                trauma_beds_available, emergency_dept_capacity, service_status, remarks
            ) VALUES (:hid, :cb, :tb, :icu, :trb, :edc, :sst, :rem)
        """),
        {
            "hid": hid, "cb": user["email"], "tb": total_beds, "icu": icu_avail,
            "trb": trauma_beds, "edc": ed_cap, "sst": svc_status,
            "rem": svc_notes or "Manual capacity update",
        },
    )
    await db.commit()
    return await hospital_my_facility(db, user)


@router.get("/hospital/incoming")
async def hospital_incoming_ambulances(
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF")),
):
    hid = _get_staff_hospital_id(user)
    res = await db.execute(
        text("""
            SELECT i.incident_id AS id, i.incident_type, i.priority, i.status, i.people_affected,
                   i.reported_at, i.dispatched_at,
                   a.ambulance_id, a.call_sign, a.vehicle_type, a.status AS ambulance_status
            FROM iers.incidents i
            LEFT JOIN iers.ambulances a ON a.ambulance_id = i.assigned_ambulance_id
            WHERE i.target_hospital_id = :hid AND i.status NOT IN ('COMPLETED', 'CANCELLED')
            ORDER BY i.reported_at DESC
        """),
        {"hid": hid},
    )
    return [dict(r) for r in res.mappings().all()]


@router.post("/hospital/confirm-arrival/{incident_id}")
async def hospital_confirm_arrival(
    incident_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF")),
):
    hid = _get_staff_hospital_id(user)
    row = (await db.execute(
        text("SELECT status, assigned_ambulance_id FROM iers.incidents WHERE incident_id = :iid AND target_hospital_id = :hid"),
        {"iid": incident_id, "hid": hid},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Incoming incident not found for your hospital")

    remarks = payload.get("remarks") or "Patient admitted and handover confirmed by hospital staff"
    await db.execute(
        text("UPDATE iers.incidents SET status = 'COMPLETED', resolved_at = NOW() WHERE incident_id = :iid"),
        {"iid": incident_id},
    )
    if row["assigned_ambulance_id"]:
        await db.execute(
            text("UPDATE iers.ambulances SET status = 'COMPLETED', updated_at = NOW() WHERE ambulance_id = :aid"),
            {"aid": row["assigned_ambulance_id"]},
        )
    await db.execute(
        text("""
            INSERT INTO iers.emergency_events (incident_id, old_status, new_status, changed_by, remarks)
            VALUES (:iid, :old, 'COMPLETED', :cb, :rem)
        """),
        {"iid": incident_id, "old": row["status"], "cb": user["email"], "rem": remarks},
    )
    await db.commit()
    return {"incident_id": incident_id, "status": "COMPLETED", "confirmed_by": user["email"]}


@router.get("/hospital/capacity-history")
async def hospital_capacity_history(
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF")),
):
    hid = _get_staff_hospital_id(user)
    res = await db.execute(
        text("""
            SELECT history_id, changed_by, total_beds, icu_available, trauma_beds_available,
                   emergency_dept_capacity, service_status, remarks, created_at
            FROM iers.hospital_capacity_history
            WHERE hospital_id = :hid
            ORDER BY created_at DESC LIMIT 50
        """),
        {"hid": hid},
    )
    return [dict(r) for r in res.mappings().all()]


# --- Hospital Bulk CSV Import Endpoints ---
@router.get("/hospital/csv/template")
async def hospital_csv_template(user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF"))):
    hid = _get_staff_hospital_id(user)
    csv_body = get_sample_csv(hid)
    return Response(
        content=csv_body,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=iers_{hid}_capacity_template.csv"},
    )


@router.post("/hospital/csv/preview")
async def hospital_csv_preview(
    payload: dict,
    user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF")),
):
    hid = _get_staff_hospital_id(user)
    csv_text = payload.get("csv_text") or ""
    if not csv_text.strip():
        raise HTTPException(status_code=400, detail="CSV content is empty")
    try:
        return parse_and_validate_csv(csv_text, hid, payload.get("mapping"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/hospital/csv/commit")
async def hospital_csv_commit(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF")),
):
    hid = _get_staff_hospital_id(user)
    csv_text = payload.get("csv_text") or ""
    filename = payload.get("filename") or "upload.csv"
    mode = payload.get("import_mode") or "UPDATE_EXISTING"
    if not csv_text.strip():
        raise HTTPException(status_code=400, detail="CSV content is empty")
    try:
        return await execute_hospital_csv_import(
            db, csv_text, filename, hid, user["email"], mode, payload.get("mapping")
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/hospital/csv/history")
async def hospital_csv_history(
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("HOSPITAL_STAFF")),
):
    hid = _get_staff_hospital_id(user)
    res = await db.execute(
        text("""
            SELECT import_id, original_filename, uploaded_by, import_mode,
                   total_rows, successful_rows, failed_rows, status, created_at
            FROM iers.hospital_csv_imports
            WHERE hospital_id = :hid
            ORDER BY created_at DESC LIMIT 30
        """),
        {"hid": hid},
    )
    return [dict(r) for r in res.mappings().all()]


# ============================================================================
# 3. AMBULANCE OPERATOR PORTAL (Strictly scoped to user's ambulance_id)
# ============================================================================
def _get_operator_ambulance_id(user: Dict[str, Any]) -> str:
    aid = user.get("assigned_entity_id")
    if not aid:
        raise HTTPException(status_code=403, detail="No ambulance_id associated with this account")
    return aid


@router.get("/operator/my-unit")
async def operator_my_unit(
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("AMBULANCE_OPERATOR")),
):
    aid = _get_operator_ambulance_id(user)
    unit = (await db.execute(
        text("""
            SELECT ambulance_id AS id, call_sign, registration_number, vehicle_type, status,
                   latitude, longitude, current_node_id, height_meters, width_meters, weight_tons,
                   driver_name, capabilities, unavailability_reason, current_speed_kmh, updated_at
            FROM iers.ambulances WHERE ambulance_id = :aid
        """),
        {"aid": aid},
    )).mappings().first()
    if not unit:
        raise HTTPException(status_code=404, detail="Assigned ambulance not found")

    active_inc = (await db.execute(
        text("""
            SELECT i.incident_id AS id, i.incident_type, i.priority, i.status, i.latitude, i.longitude,
                   i.description, i.people_affected, i.reported_at, i.nearest_node_id,
                   h.name AS hospital_name, h.latitude AS hosp_lat, h.longitude AS hosp_lon,
                   h.nearest_node_id AS hosp_node_id
            FROM iers.incidents i
            LEFT JOIN iers.hospitals h ON h.hospital_id = i.target_hospital_id
            WHERE i.assigned_ambulance_id = :aid AND i.status NOT IN ('COMPLETED', 'CANCELLED')
            ORDER BY i.reported_at DESC LIMIT 1
        """),
        {"aid": aid},
    )).mappings().first()

    return {
        "unit": dict(unit),
        "active_assignment": dict(active_inc) if active_inc else None,
        "allowed_next_states": sorted(list(VALID_TRANSITIONS.get(unit["status"], set()))),
    }


@router.post("/operator/status")
async def operator_update_status(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("AMBULANCE_OPERATOR")),
):
    aid = _get_operator_ambulance_id(user)
    new_state = (payload.get("status") or "").upper()
    reason = (payload.get("reason") or "").strip()
    if new_state not in OPERATOR_STATES:
        raise HTTPException(status_code=400, detail=f"Invalid state '{new_state}'")

    cur = (await db.execute(
        text("SELECT status FROM iers.ambulances WHERE ambulance_id = :aid"),
        {"aid": aid},
    )).first()
    if not cur:
        raise HTTPException(status_code=404, detail="Ambulance not found")
    old_state = cur[0]

    # Check active assignment guard before marking AVAILABLE
    active = (await db.execute(
        text("SELECT incident_id FROM iers.incidents WHERE assigned_ambulance_id = :aid AND status NOT IN ('COMPLETED', 'CANCELLED') LIMIT 1"),
        {"aid": aid},
    )).first()
    if new_state == "AVAILABLE" and active:
        raise HTTPException(status_code=400, detail="Cannot mark ambulance AVAILABLE while an active assignment is open")

    allowed = VALID_TRANSITIONS.get(old_state, set())
    if new_state != old_state and new_state not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition from '{old_state}' to '{new_state}'. Allowed: {', '.join(sorted(allowed))}",
        )

    await db.execute(
        text("""
            UPDATE iers.ambulances
            SET status = :st,
                unavailability_reason = CASE WHEN :st = 'UNAVAILABLE' THEN :rsn ELSE '' END,
                updated_at = NOW()
            WHERE ambulance_id = :aid
        """),
        {"st": new_state, "rsn": reason, "aid": aid},
    )

    if active and new_state in ("ACCEPTED", "EN_ROUTE", "ARRIVED_AT_SCENE", "PATIENT_PICKED_UP", "EN_ROUTE_TO_HOSPITAL", "ARRIVED_AT_HOSPITAL", "COMPLETED"):
        inc_id = active[0]
        await db.execute(
            text("""
                UPDATE iers.incidents
                SET status = :st, resolved_at = CASE WHEN :st = 'COMPLETED' THEN NOW() ELSE resolved_at END
                WHERE incident_id = :iid
            """),
            {"st": new_state, "iid": inc_id},
        )
        await db.execute(
            text("""
                INSERT INTO iers.emergency_events (incident_id, old_status, new_status, changed_by, remarks)
                VALUES (:iid, :old, :new, :cb, :rem)
            """),
            {"iid": inc_id, "old": old_state, "new": new_state, "cb": user["email"], "rem": reason or f"Operator transitioned to {new_state}"},
        )

    await db.commit()
    return {"ambulance_id": aid, "old_status": old_state, "new_status": new_state}


@router.post("/operator/location")
async def operator_update_location(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("AMBULANCE_OPERATOR", "DISPATCHER")),
):
    aid = payload.get("ambulance_id") if user["role"] == "DISPATCHER" else _get_operator_ambulance_id(user)
    lat = float(payload["latitude"])
    lon = float(payload["longitude"])
    speed = float(payload.get("speed_kmh", 0.0))
    bearing = float(payload.get("bearing_deg", 0.0))
    is_sim = bool(payload.get("is_simulated", True))

    nearest = await find_nearest_road_node(db, lat, lon)
    nid = str(nearest[0]) if nearest else None

    await db.execute(
        text("""
            UPDATE iers.ambulances
            SET latitude = :lat, longitude = :lon, current_node_id = :nid,
                current_speed_kmh = :spd,
                geom = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
                updated_at = NOW()
            WHERE ambulance_id = :aid
        """),
        {"lat": lat, "lon": lon, "nid": nid, "spd": speed, "aid": aid},
    )
    await db.execute(
        text("""
            INSERT INTO iers.ambulance_location_history (ambulance_id, latitude, longitude, speed_kmh, bearing_deg, is_simulated, geom)
            VALUES (:aid, :lat, :lon, :spd, :brg, :sim, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))
        """),
        {"aid": aid, "lat": lat, "lon": lon, "spd": speed, "brg": bearing, "sim": is_sim},
    )
    await db.commit()
    return {"ambulance_id": aid, "latitude": lat, "longitude": lon, "current_node_id": nid, "is_simulated": is_sim}


@router.get("/operator/history")
async def operator_assignment_history(
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("AMBULANCE_OPERATOR")),
):
    aid = _get_operator_ambulance_id(user)
    res = await db.execute(
        text("""
            SELECT i.incident_id AS id, i.incident_type, i.priority, i.status,
                   i.reported_at, i.resolved_at, h.name AS hospital_name
            FROM iers.incidents i
            LEFT JOIN iers.hospitals h ON h.hospital_id = i.target_hospital_id
            WHERE i.assigned_ambulance_id = :aid
            ORDER BY i.reported_at DESC LIMIT 30
        """),
        {"aid": aid},
    )
    return [dict(r) for r in res.mappings().all()]


# ============================================================================
# 4. DISPATCHER EXTENSIONS: BBOX ZOOM-DEPENDENT ROADS & STORAGE TRANSPARENCY
# ============================================================================
@router.get("/gis/roads-bbox")
async def get_roads_by_bbox(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    zoom: int = 12,
    db: AsyncSession = Depends(get_db),
):
    """
    Zoom-dependent, bounding-box PostGIS query so the browser never downloads all 40,394 roads at once:
    - zoom <= 12: trunk & primary highways
    - zoom 13-14: trunk, primary, secondary, tertiary
    - zoom >= 15: all roads including residential/service
    """
    if zoom <= 12:
        hw_filter = "highway IN ('trunk', 'trunk_link', 'primary', 'primary_link')"
    elif zoom <= 14:
        hw_filter = "highway IN ('trunk', 'trunk_link', 'primary', 'primary_link', 'secondary', 'secondary_link', 'tertiary')"
    else:
        hw_filter = "highway IS NOT NULL"

    res = await db.execute(
        text(f"""
            SELECT road_id, name, highway, is_oneway, emergency_contraflow_allowed,
                   is_blocked, is_emergency_corridor, traffic_multiplier,
                   ST_AsGeoJSON(geom)::json AS geometry
            FROM iers.roads
            WHERE geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
              AND {hw_filter}
            LIMIT 1800
        """),
        {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat},
    )
    features = []
    for r in res.mappings().all():
        features.append({
            "type": "Feature",
            "geometry": r["geometry"],
            "properties": {
                "road_id": r["road_id"],
                "name": r["name"] or f"{r['highway']} road",
                "highway": r["highway"],
                "is_oneway": bool(r["is_oneway"]),
                "emergency_contraflow_allowed": bool(r["emergency_contraflow_allowed"]),
                "is_blocked": bool(r["is_blocked"]),
                "is_emergency_corridor": bool(r["is_emergency_corridor"]),
                "traffic_multiplier": float(r["traffic_multiplier"] or 1.0),
            },
        })
    return {"type": "FeatureCollection", "zoom": zoom, "count": len(features), "features": features}


@router.get("/system/storage-architecture")
async def get_storage_architecture(
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("DISPATCHER")),
):
    """Read-only Data Architecture & Storage Transparency inventory (zero secrets)."""
    tables_meta = [
        ("roads", "Imported GIS Source + Dynamic Traffic Weights", "geom (LineString, 4326)", "Persistent"),
        ("road_nodes", "Routing Graph Vertices (Hubballi-Dharwad)", "location (Point, 4326)", "Persistent"),
        ("roads_raw", "Original OpenStreetMap Road Geometries", "geom (LineString, 4326)", "Imported GIS Source"),
        ("hospitals", "Real OSM Hospital Coordinates + Prototype Capacity", "geom (Point, 4326)", "Persistent + Simulated Capacity"),
        ("ambulances", "Fleet Units, Capabilities & Live Position", "geom (Point, 4326)", "Persistent"),
        ("incidents", "Citizen/Dispatcher Emergency Reports & Queue", "geom (Point, 4326)", "Persistent"),
        ("emergency_events", "Audit Timeline of Emergency Status Changes", "None", "Persistent"),
        ("route_history", "Calculated pgRouting Dijkstra/A* Paths", "route_geom (Geometry, 4326)", "Persistent"),
        ("hospital_capacity_history", "Hospital Staff Capacity Update Audit Log", "None", "Persistent"),
        ("hospital_csv_imports", "Hospital Bulk CSV Import Audit & Validation", "None", "Persistent"),
        ("ambulance_location_history", "GPS / Simulated Telemetry Trail", "geom (Point, 4326)", "Persistent / Simulated"),
        ("users", "Authenticated Stakeholder Accounts & Roles", "None", "Persistent"),
    ]
    inventory = []
    for tname, purpose, spatial_col, category in tables_meta:
        cnt = (await db.execute(text(f"SELECT count(*) FROM iers.{tname}"))).scalar()
        inventory.append({
            "table": f"iers.{tname}",
            "purpose": purpose,
            "spatial_column": spatial_col,
            "category": category,
            "row_count": int(cnt or 0),
        })

    return {
        "database_engine": "PostgreSQL 17.5 + PostGIS 3.5.2 + pgRouting 3.7.3",
        "gis_basemap": "OpenStreetMap Standard Labelled Tiles + Native PyQGIS Project Renderer",
        "inventory": inventory,
    }
