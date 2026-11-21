"""
IERS Complete 7-Stage End-to-End Emergency Workflow & Isolated Telemetry Simulator
- Stage 2: Spatial analysis & real pgRouting ambulance ranking (ETAs in seconds)
- Stage 3: Multi-criteria hospital recommendation (network travel time + beds/ICU/trauma)
- Stage 4: Dispatcher atomic assignment with override reason
- Stage 5 & 6: Two-leg routing (Leg 1: Amb->Scene, Leg 2: Scene->Hospital) + automatic movement
- Stage 7: Hospital handover & Dispatcher completion verification (ambulance released ONLY on approval)
- Citizen Complaints & Dispatcher Resolution
"""
import asyncio
import json
import math
from datetime import datetime
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.database import get_db, AsyncSessionLocal
from app.core.auth import require_roles
from app.services.pgrouting_service import compute_pgrouting_path

router = APIRouter(prefix="/api/v1", tags=["End-to-End Workflow"])

# Atomic lock preventing two emergencies from reserving the same ambulance
workflow_dispatch_lock = asyncio.Lock()

# Isolated prototype telemetry simulator tasks (can be swapped for real GPS later)
_active_sim_tasks: Dict[str, asyncio.Task] = {}
_ws_broadcaster = None


def register_ws_broadcaster(fn):
    global _ws_broadcaster
    _ws_broadcaster = fn


async def _broadcast(msg: dict):
    if _ws_broadcaster:
        await _ws_broadcaster(msg)


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return round((math.atan2(lon2 - lon1, lat2 - lat1) * 180.0 / math.pi + 360.0) % 360.0, 1)


def stop_telemetry_sim(ambulance_id: str):
    t = _active_sim_tasks.pop(ambulance_id, None)
    if t and not t.done():
        t.cancel()


async def _run_route_telemetry_sim(
    incident_id: str,
    ambulance_id: str,
    coordinates: List[List[float]],
    total_dist_m: float,
    leg_name: str,
):
    """
    Isolated prototype telemetry simulator:
    Moves ambulance smoothly along the ACTUAL pgRouting road coordinates,
    persisting coordinates & remaining ETA (in seconds) to PostgreSQL
    and broadcasting WebSocket updates to Dispatcher, Crew, and Citizen.
    """
    if not coordinates or len(coordinates) < 2:
        return
    # Sample ~18 smooth steps along the real road polyline (~18s demo journey)
    step_count = min(18, len(coordinates) - 1)
    indices = [int(round(i * (len(coordinates) - 1) / step_count)) for i in range(step_count + 1)]

    try:
        for step_idx in range(1, len(indices)):
            await asyncio.sleep(1.0)
            p_prev = coordinates[indices[step_idx - 1]]
            p_curr = coordinates[indices[step_idx]]
            lat, lon = float(p_curr[0]), float(p_curr[1])
            brg = _bearing_deg(p_prev[0], p_prev[1], lat, lon)
            frac_left = max(0.0, 1.0 - (step_idx / float(step_count)))
            rem_dist = round(total_dist_m * frac_left, 1)
            eta_s = max(0, int(round((step_count - step_idx) * 1.0)))

            async with AsyncSessionLocal() as db:
                await db.execute(
                    text("""
                        UPDATE iers.ambulances
                        SET latitude = :lat, longitude = :lon, current_speed_kmh = 52.0,
                            geom = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), updated_at = NOW()
                        WHERE ambulance_id = :aid
                    """),
                    {"lat": lat, "lon": lon, "aid": ambulance_id},
                )
                await db.execute(
                    text("""
                        UPDATE iers.incidents
                        SET eta_seconds = :eta, remaining_distance_m = :rem
                        WHERE incident_id = :iid
                    """),
                    {"eta": eta_s, "rem": rem_dist, "iid": incident_id},
                )
                await db.execute(
                    text("""
                        INSERT INTO iers.ambulance_location_history
                            (ambulance_id, latitude, longitude, speed_kmh, bearing_deg, is_simulated, geom)
                        VALUES (:aid, :lat, :lon, 52.0, :brg, TRUE, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))
                    """),
                    {"aid": ambulance_id, "lat": lat, "lon": lon, "brg": brg},
                )
                await db.commit()

            await _broadcast({
                "type": "AMBULANCE_LOCATION",
                "incident_id": incident_id,
                "ambulance_id": ambulance_id,
                "leg": leg_name,
                "latitude": lat,
                "longitude": lon,
                "bearing_deg": brg,
                "remaining_distance_m": rem_dist,
                "eta_seconds": eta_s,
                "is_simulated": True,
            })
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[TELEMETRY-SIM] Stopped for {ambulance_id}: {e}")


# ============================================================================
# STAGE 2 & 3: SPATIAL ANALYSIS — RANKED AMBULANCES & HOSPITAL SUITABILITY
# ============================================================================
@router.get("/workflow/analyze/{incident_id}")
async def analyze_emergency_spatial(
    incident_id: str,
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("DISPATCHER", "AMBULANCE_OPERATOR")),
):
    """
    Runs real pgRouting (pgr_astar) from every available ambulance to the emergency
    and from the emergency to candidate hospitals. Ranks by actual road-network
    travel time in SECONDS + medical capability suitability.
    """
    inc = (await db.execute(
        text("SELECT incident_id, incident_type, priority, latitude, longitude, nearest_node_id FROM iers.incidents WHERE incident_id = :iid"),
        {"iid": incident_id},
    )).mappings().first()
    if not inc or not inc["nearest_node_id"]:
        raise HTTPException(status_code=404, detail="Incident or snapped road node not found")
    inc_node = int(inc["nearest_node_id"])

    # 1. Rank available ambulances by actual road-network travel time
    ambs = (await db.execute(
        text("""
            SELECT ambulance_id, call_sign, vehicle_type, status, latitude, longitude,
                   current_node_id, height_meters, width_meters, weight_tons, capabilities
            FROM iers.ambulances
            WHERE status = 'AVAILABLE' AND current_node_id IS NOT NULL
        """)
    )).mappings().all()

    ranked_ambulances = []
    for a in ambs:
        route = await compute_pgrouting_path(
            db, int(a["current_node_id"]), inc_node,
            algorithm="ASTAR", vehicle_mode="AMBULANCE",
            vehicle_height=float(a["height_meters"] or 2.4),
            vehicle_width=float(a["width_meters"] or 2.2),
            vehicle_weight=float(a["weight_tons"] or 3.5),
        )
        if not route:
            continue
        # Demo-scaled ETA in seconds (also preserves real network duration)
        eta_s = max(12, int(round(route["total_duration_seconds"])))
        cap_bonus = 0 if (inc["priority"] == "P1" and a["vehicle_type"] == "ALS") else 60
        ranked_ambulances.append({
            "ambulance_id": a["ambulance_id"],
            "call_sign": a["call_sign"],
            "vehicle_type": a["vehicle_type"],
            "status": a["status"],
            "latitude": a["latitude"],
            "longitude": a["longitude"],
            "capabilities": a["capabilities"],
            "distance_m": route["total_distance_meters"],
            "eta_seconds": eta_s,
            "sort_score": eta_s + cap_bonus,
            "reason": (
                f"Fastest network route ({eta_s}s, {route['total_distance_km']} km)"
                + (" + ALS match for P1" if inc["priority"] == "P1" and a["vehicle_type"] == "ALS" else "")
            ),
            "route": route,
        })

    ranked_ambulances.sort(key=lambda x: x["sort_score"])
    for idx, item in enumerate(ranked_ambulances, start=1):
        item["rank"] = idx

    # 2. Rank candidate hospitals by road-network travel time + beds/ICU/trauma suitability
    hosps = (await db.execute(
        text("""
            SELECT hospital_id, name, city, latitude, longitude, nearest_node_id,
                   total_beds, icu_available, trauma_beds_available, trauma_level,
                   has_icu, has_trauma_care, has_cardiology, service_status, attributes_simulated
            FROM iers.hospitals
            WHERE nearest_node_id IS NOT NULL AND service_status = 'OPERATIONAL' AND icu_available > 0
            ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
            LIMIT 6
        """),
        {"lat": inc["latitude"], "lon": inc["longitude"]},
    )).mappings().all()

    ranked_hospitals = []
    for h in hosps:
        route = await compute_pgrouting_path(
            db, inc_node, int(h["nearest_node_id"]), algorithm="ASTAR", vehicle_mode="AMBULANCE"
        )
        if not route:
            continue
        eta_s = max(15, int(round(route["total_duration_seconds"])))
        score = 1000.0 - eta_s * 0.5
        reasons = [f"Network ETA {eta_s}s ({route['total_distance_km']} km)", f"{h['icu_available']} ICU beds"]
        if inc["priority"] == "P1" and h["has_trauma_care"]:
            score += 180.0
            reasons.append("Level-1 Trauma capable")
        if h["has_cardiology"]:
            score += 40.0
            reasons.append("Cardiology")
        ranked_hospitals.append({
            "hospital_id": h["hospital_id"],
            "name": h["name"],
            "city": h["city"],
            "latitude": h["latitude"],
            "longitude": h["longitude"],
            "total_beds": h["total_beds"],
            "icu_available": h["icu_available"],
            "trauma_level": h["trauma_level"],
            "has_trauma_care": h["has_trauma_care"],
            "has_cardiology": h["has_cardiology"],
            "attributes_simulated": h["attributes_simulated"],
            "distance_m": route["total_distance_meters"],
            "eta_seconds": eta_s,
            "suitability_score": round(score, 1),
            "reason": " · ".join(reasons),
            "route": route,
        })

    ranked_hospitals.sort(key=lambda x: x["suitability_score"], reverse=True)
    for idx, item in enumerate(ranked_hospitals, start=1):
        item["rank"] = idx

    return {
        "incident": dict(inc),
        "ranked_ambulances": ranked_ambulances,
        "ranked_hospitals": ranked_hospitals,
    }


# ============================================================================
# STAGE 4: DISPATCHER ATOMIC ASSIGNMENT (WITH OVERRIDE REASON)
# ============================================================================
@router.post("/workflow/assign/{incident_id}")
async def dispatcher_assign_ambulance(
    incident_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("DISPATCHER")),
):
    ambulance_id = payload.get("ambulance_id")
    hospital_id = payload.get("hospital_id")
    override_reason = (payload.get("override_reason") or "").strip()
    if not ambulance_id or not hospital_id:
        raise HTTPException(status_code=400, detail="ambulance_id and hospital_id are required")

    async with workflow_dispatch_lock:
        inc = (await db.execute(
            text("SELECT incident_id, status, nearest_node_id FROM iers.incidents WHERE incident_id = :iid"),
            {"iid": incident_id},
        )).mappings().first()
        if not inc:
            raise HTTPException(status_code=404, detail="Incident not found")

        amb = (await db.execute(
            text("SELECT ambulance_id, call_sign, status, current_node_id, height_meters, width_meters, weight_tons FROM iers.ambulances WHERE ambulance_id = :aid"),
            {"aid": ambulance_id},
        )).mappings().first()
        if not amb or amb["status"] != "AVAILABLE":
            raise HTTPException(status_code=409, detail="Selected ambulance is no longer AVAILABLE")

        hosp = (await db.execute(
            text("SELECT hospital_id, name, nearest_node_id FROM iers.hospitals WHERE hospital_id = :hid"),
            {"hid": hospital_id},
        )).mappings().first()
        if not hosp:
            raise HTTPException(status_code=404, detail="Selected hospital not found")

        leg1 = await compute_pgrouting_path(
            db, int(amb["current_node_id"]), int(inc["nearest_node_id"]),
            algorithm="ASTAR", vehicle_mode="AMBULANCE",
            vehicle_height=float(amb["height_meters"] or 2.4),
            vehicle_width=float(amb["width_meters"] or 2.2),
            vehicle_weight=float(amb["weight_tons"] or 3.5),
        )
        if not leg1:
            raise HTTPException(status_code=400, detail="No viable road route from ambulance to emergency")

        eta_s = max(12, int(round(leg1["total_duration_seconds"])))
        await db.execute(
            text("UPDATE iers.ambulances SET status = 'ASSIGNED', updated_at = NOW() WHERE ambulance_id = :aid"),
            {"aid": ambulance_id},
        )
        await db.execute(
            text("""
                UPDATE iers.incidents
                SET assigned_ambulance_id = :aid, target_hospital_id = :hid,
                    status = 'ASSIGNED', active_leg = 'LEG_1',
                    active_route_json = CAST(:rjson AS jsonb),
                    eta_seconds = :eta, remaining_distance_m = :rem,
                    override_reason = :ovr, dispatched_at = NOW()
                WHERE incident_id = :iid
            """),
            {
                "aid": ambulance_id, "hid": hospital_id,
                "rjson": json.dumps(leg1), "eta": eta_s, "rem": leg1["total_distance_meters"],
                "ovr": override_reason, "iid": incident_id,
            },
        )
        await db.execute(
            text("""
                INSERT INTO iers.emergency_events (incident_id, old_status, new_status, changed_by, remarks)
                VALUES (:iid, :old, 'ASSIGNED', :cb, :rem)
            """),
            {
                "iid": incident_id, "old": inc["status"], "cb": user["email"],
                "rem": f"Dispatcher assigned {amb['call_sign']} -> {hosp['name']} (Leg 1: {eta_s}s)"
                       + (f" | Override: {override_reason}" if override_reason else ""),
            },
        )
        await db.execute(
            text("""
                INSERT INTO iers.route_history (incident_id, ambulance_id, algorithm, vehicle_mode, start_node, end_node, distance_m, estimated_time_s, contraflow_edges, route_geom)
                VALUES (:iid, :aid, 'ASTAR', 'AMBULANCE', :sn, :en, :dm, :ts, :cf, ST_SetSRID(ST_GeomFromGeoJSON(:gjson), 4326))
            """),
            {
                "iid": incident_id, "aid": ambulance_id, "sn": leg1["start_node"], "en": leg1["end_node"],
                "dm": leg1["total_distance_meters"], "ts": leg1["total_duration_seconds"],
                "cf": leg1["contraflow_edges_used"], "gjson": json.dumps(leg1["geojson"]["geometry"]),
            },
        )
        await db.commit()

    await _broadcast({"type": "DISPATCH_ASSIGNED", "incident_id": incident_id, "ambulance_id": ambulance_id, "hospital_id": hospital_id, "eta_seconds": eta_s})
    return {"status": "ASSIGNED", "incident_id": incident_id, "ambulance_id": ambulance_id, "hospital_id": hospital_id, "leg1_route": leg1, "eta_seconds": eta_s}


# ============================================================================
# STAGE 5 & 6: AMBULANCE CREW ACTIONS (TWO-LEG ROUTING + AUTOMATIC MOVEMENT)
# ============================================================================
@router.post("/workflow/crew-action/{incident_id}")
async def crew_workflow_action(
    incident_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("AMBULANCE_OPERATOR", "DISPATCHER")),
):
    action = (payload.get("action") or "").upper()
    inc = (await db.execute(
        text("""
            SELECT i.incident_id, i.status, i.assigned_ambulance_id, i.target_hospital_id,
                   i.nearest_node_id, i.active_route_json,
                   h.nearest_node_id AS hosp_node_id, h.name AS hospital_name
            FROM iers.incidents i
            LEFT JOIN iers.hospitals h ON h.hospital_id = i.target_hospital_id
            WHERE i.incident_id = :iid
        """),
        {"iid": incident_id},
    )).mappings().first()
    if not inc or not inc["assigned_ambulance_id"]:
        raise HTTPException(status_code=404, detail="Assigned incident not found")

    aid = inc["assigned_ambulance_id"]
    if user["role"] == "AMBULANCE_OPERATOR" and user.get("assigned_entity_id") != aid:
        raise HTTPException(status_code=403, detail="Not authorized for this ambulance assignment")

    new_status = None
    leg2_route = None

    if action == "ACCEPT_AND_START":
        new_status = "EN_ROUTE_TO_EMERGENCY"
        route = inc["active_route_json"]
        if route and route.get("coordinates"):
            stop_telemetry_sim(aid)
            _active_sim_tasks[aid] = asyncio.create_task(
                _run_route_telemetry_sim(incident_id, aid, route["coordinates"], route["total_distance_meters"], "LEG_1")
            )
    elif action == "ARRIVED_AT_EMERGENCY":
        stop_telemetry_sim(aid)
        new_status = "ARRIVED_AT_EMERGENCY"
    elif action == "PATIENT_PICKED_UP":
        stop_telemetry_sim(aid)
        new_status = "PATIENT_PICKED_UP"
        # Automatically calculate LEG 2: Emergency -> Recommended Hospital
        if inc["nearest_node_id"] and inc["hosp_node_id"]:
            leg2_route = await compute_pgrouting_path(
                db, int(inc["nearest_node_id"]), int(inc["hosp_node_id"]),
                algorithm="ASTAR", vehicle_mode="AMBULANCE",
            )
            if leg2_route:
                await db.execute(
                    text("""
                        UPDATE iers.incidents
                        SET active_leg = 'LEG_2', active_route_json = CAST(:rjson AS jsonb),
                            eta_seconds = :eta, remaining_distance_m = :rem
                        WHERE incident_id = :iid
                    """),
                    {
                        "rjson": json.dumps(leg2_route),
                        "eta": max(15, int(round(leg2_route["total_duration_seconds"]))),
                        "rem": leg2_route["total_distance_meters"],
                        "iid": incident_id,
                    },
                )
    elif action == "START_HOSPITAL_JOURNEY":
        new_status = "EN_ROUTE_TO_HOSPITAL"
        route = inc["active_route_json"]
        if route and route.get("coordinates"):
            stop_telemetry_sim(aid)
            _active_sim_tasks[aid] = asyncio.create_task(
                _run_route_telemetry_sim(incident_id, aid, route["coordinates"], route["total_distance_meters"], "LEG_2")
            )
    elif action == "ARRIVED_AT_HOSPITAL":
        stop_telemetry_sim(aid)
        new_status = "ARRIVED_AT_HOSPITAL"
    elif action == "SUBMIT_COMPLETION":
        stop_telemetry_sim(aid)
        new_status = "PENDING_VERIFICATION"
        await db.execute(
            text("UPDATE iers.incidents SET verification_status = 'PENDING' WHERE incident_id = :iid"),
            {"iid": incident_id},
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown crew action: {action}")

    await db.execute(
        text("UPDATE iers.incidents SET status = :st WHERE incident_id = :iid"),
        {"st": new_status, "iid": incident_id},
    )
    # Ambulance remains occupied (NOT AVAILABLE) until Dispatcher approves verification!
    await db.execute(
        text("UPDATE iers.ambulances SET status = :st, updated_at = NOW() WHERE ambulance_id = :aid"),
        {"st": new_status, "aid": aid},
    )
    await db.execute(
        text("""
            INSERT INTO iers.emergency_events (incident_id, old_status, new_status, changed_by, remarks)
            VALUES (:iid, :old, :new, :cb, :rem)
        """),
        {"iid": incident_id, "old": inc["status"], "new": new_status, "cb": user["email"], "rem": f"Crew action: {action}"},
    )
    await db.commit()
    await _broadcast({"type": "WORKFLOW_STATE", "incident_id": incident_id, "ambulance_id": aid, "status": new_status})
    return {"incident_id": incident_id, "status": new_status, "leg2_route": leg2_route}


# ============================================================================
# STAGE 7: DISPATCHER COMPLETION VERIFICATION (RELEASES AMBULANCE TO AVAILABLE)
# ============================================================================
@router.post("/workflow/verify/{incident_id}")
async def dispatcher_verify_completion(
    incident_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("DISPATCHER")),
):
    decision = (payload.get("decision") or "APPROVE").upper()
    notes = (payload.get("notes") or "").strip()

    inc = (await db.execute(
        text("SELECT status, assigned_ambulance_id FROM iers.incidents WHERE incident_id = :iid"),
        {"iid": incident_id},
    )).mappings().first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    if decision == "APPROVE":
        await db.execute(
            text("""
                UPDATE iers.incidents
                SET status = 'COMPLETED', verification_status = 'APPROVED',
                    verified_by = :vb, verification_notes = :nt, resolved_at = NOW()
                WHERE incident_id = :iid
            """),
            {"vb": user["email"], "nt": notes or "Completion verified by dispatcher", "iid": incident_id},
        )
        if inc["assigned_ambulance_id"]:
            stop_telemetry_sim(inc["assigned_ambulance_id"])
            await db.execute(
                text("UPDATE iers.ambulances SET status = 'AVAILABLE', updated_at = NOW() WHERE ambulance_id = :aid"),
                {"aid": inc["assigned_ambulance_id"]},
            )
        new_st = "COMPLETED"
    else:
        await db.execute(
            text("""
                UPDATE iers.incidents
                SET status = 'ARRIVED_AT_HOSPITAL', verification_status = 'REJECTED',
                    verified_by = :vb, verification_notes = :nt
                WHERE incident_id = :iid
            """),
            {"vb": user["email"], "nt": notes or "Verification rejected — requires follow-up", "iid": incident_id},
        )
        new_st = "ARRIVED_AT_HOSPITAL"

    await db.execute(
        text("""
            INSERT INTO iers.emergency_events (incident_id, old_status, new_status, changed_by, remarks)
            VALUES (:iid, :old, :new, :cb, :rem)
        """),
        {"iid": incident_id, "old": inc["status"], "new": new_st, "cb": user["email"], "rem": f"Dispatcher {decision}: {notes}"},
    )
    await db.commit()
    await _broadcast({"type": "VERIFICATION_COMPLETE", "incident_id": incident_id, "decision": decision, "status": new_st})
    return {"incident_id": incident_id, "decision": decision, "status": new_st}


# ============================================================================
# COMPLAINTS (Citizen Submission + Dispatcher Resolution)
# ============================================================================
@router.post("/complaints")
async def create_complaint(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("CITIZEN")),
):
    cid = f"CMP_{int(datetime.utcnow().timestamp() * 1000)}"
    await db.execute(
        text("""
            INSERT INTO iers.complaints (complaint_id, incident_id, submitted_by, category, description)
            VALUES (:cid, :iid, :sub, :cat, :desc)
        """),
        {
            "cid": cid,
            "iid": payload.get("incident_id") or None,
            "sub": user["email"],
            "cat": payload.get("category") or "RESPONSE_DELAY",
            "desc": payload.get("description") or "",
        },
    )
    await db.commit()
    return {"complaint_id": cid, "status": "OPEN"}


@router.get("/complaints")
async def list_complaints(
    db: AsyncSession = Depends(get_db),
    user: Dict[str, Any] = Depends(require_roles("CITIZEN", "DISPATCHER")),
):
    if user["role"] == "CITIZEN":
        res = await db.execute(
            text("SELECT * FROM iers.complaints WHERE submitted_by = :sub ORDER BY created_at DESC"),
            {"sub": user["email"]},
        )
    else:
        res = await db.execute(text("SELECT * FROM iers.complaints ORDER BY created_at DESC"))
    return [dict(r) for r in res.mappings().all()]


@router.patch("/complaints/{complaint_id}")
async def resolve_complaint(
    complaint_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _: Dict[str, Any] = Depends(require_roles("DISPATCHER")),
):
    st = (payload.get("status") or "RESOLVED").upper()
    resp = payload.get("dispatcher_response") or "Reviewed by dispatcher"
    await db.execute(
        text("""
            UPDATE iers.complaints
            SET status = :st, dispatcher_response = :resp,
                resolved_at = CASE WHEN :st = 'RESOLVED' THEN NOW() ELSE resolved_at END
            WHERE complaint_id = :cid
        """),
        {"st": st, "resp": resp, "cid": complaint_id},
    )
    await db.commit()
    return {"complaint_id": complaint_id, "status": st}
