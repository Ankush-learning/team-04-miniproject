"""
Live Operations Simulator for IERS (Hubballi).
Spawns realistic random emergencies from multiple seeded Hubballi citizens,
auto-assigns the nearest available ambulance via PostGIS KNN + pgRouting A*,
and drives each ambulance autonomously through both legs (Leg 1: to scene,
Leg 2: to nearest hospital) while continuously updating PostgreSQL and
broadcasting live telemetry over WebSockets.

Manual mode remains 100% unaffected: the simulator only manages emergencies
it spawns (leaving manual citizen reports and manual dispatcher overrides intact).
"""
import asyncio
import json
import random
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.core.auth import hash_password
from app.core.database import AsyncSessionLocal
from app.services.pgrouting_service import compute_pgrouting_path

SIM_CITIZENS = [
    ("citizen2@iers.gov.in", "Aarav Kulkarni", "Vidyanagar"),
    ("citizen3@iers.gov.in", "Priya Deshpande", "Gokul Road"),
    ("citizen4@iers.gov.in", "Rohan Patil", "Keshwapur"),
    ("citizen5@iers.gov.in", "Sneha Joshi", "Unkal Lake"),
    ("citizen6@iers.gov.in", "Vikram Naik", "Deshpande Nagar"),
    ("citizen7@iers.gov.in", "Ananya Hegde", "Shirur Park"),
    ("citizen8@iers.gov.in", "Karthik Shenoy", "Old Hubballi"),
    ("citizen9@iers.gov.in", "Meera Nadkarni", "Navanagar"),
]

EMERGENCY_TEMPLATES = [
    ("P1", "CARDIAC", "Acute Cardiac Arrest"),
    ("P1", "TRAUMA", "Multi-Vehicle Road Traffic Accident"),
    ("P1", "STROKE", "Suspected Acute Ischemic Stroke"),
    ("P2", "RESPIRATORY", "Severe Respiratory Distress"),
    ("P2", "OBSTETRIC", "High-Risk Obstetric Emergency"),
    ("P2", "TRAUMA", "Industrial Construction Fall Trauma"),
    ("P3", "MEDICAL", "Hypertensive Crisis & Chest Pain"),
]

_sim_running: bool = False
_sim_loop_task: Optional[asyncio.Task] = None
_active_mission_tasks: Dict[str, asyncio.Task] = {}
_stats: Dict[str, Any] = {
    "started_at": None,
    "spawned_count": 0,
    "completed_count": 0,
    "max_concurrent": 4,
    "spawn_interval_s": 8,
}


async def _broadcast(payload: dict):
    try:
        from app.main import manager
        await manager.broadcast(payload)
    except Exception:
        pass


async def ensure_simulated_citizens():
    """Idempotently seed multiple Hubballi citizen accounts into public.users."""
    pw_hash = hash_password("citizen123")
    async with AsyncSessionLocal() as db:
        for email, full_name, _area in SIM_CITIZENS:
            await db.execute(
                text("""
                    INSERT INTO public.users (email, password_hash, name, role, is_active)
                    VALUES (:email, :pw, :name, 'CITIZEN', TRUE)
                    ON CONFLICT (email) DO NOTHING
                """),
                {"email": email, "pw": pw_hash, "name": full_name},
            )
        await db.commit()


async def _drive_leg(eid: str, aid: str, coords: List[List[float]], total_dist_m: float, leg_name: str):
    """Smoothly step an ambulance along real pgRouting road coordinates, updating PostgreSQL & WS."""
    if not coords:
        return
    # Smooth slow steps (~35 steps x 1.8s = ~60s per leg) so faculty can clearly watch every turn
    max_steps = 35
    stride = max(1, len(coords) // max_steps)
    sampled = coords[::stride]
    if sampled[-1] != coords[-1]:
        sampled.append(coords[-1])

    total_pts = len(sampled)
    for idx, pt in enumerate(sampled):
        if not _sim_running:
            return
        lat, lon = float(pt[0]), float(pt[1])
        frac = (idx + 1) / total_pts
        rem_m = max(0.0, total_dist_m * (1.0 - frac))
        eta_s = max(0, int(rem_m / 12.0))

        async with AsyncSessionLocal() as db:
            await db.execute(
                text("""
                    UPDATE public.ambulances
                    SET current_location = ST_SetSRID(ST_MakePoint(CAST(:lon AS double precision), CAST(:lat AS double precision)), 4326),
                        last_location_update = NOW()
                    WHERE ambulance_id = CAST(:aid AS uuid)
                """),
                {"lon": lon, "lat": lat, "aid": aid},
            )
            await db.execute(
                text("""
                    INSERT INTO public.ambulance_location_history (ambulance_id, latitude, longitude, is_simulated)
                    VALUES (CAST(:aid AS uuid), :lat, :lon, TRUE)
                """),
                {"aid": aid, "lon": lon, "lat": lat},
            )
            await db.execute(
                text("""
                    UPDATE public.emergencies
                    SET eta_seconds = :eta, remaining_distance_m = :rem
                    WHERE emergency_id = CAST(:eid AS uuid)
                """),
                {"eta": eta_s, "rem": round(rem_m, 1), "eid": eid},
            )
            await db.commit()

        await _broadcast({
            "type": "AMBULANCE_LOCATION",
            "ambulance_id": aid,
            "incident_id": eid,
            "lat": lat,
            "lon": lon,
            "eta_seconds": eta_s,
            "remaining_distance_m": round(rem_m, 1),
            "leg": leg_name,
        })
        await asyncio.sleep(1.8)


async def _run_autonomous_incident(eid: str):
    """Drives a single simulated emergency through Dispatch -> Leg 1 -> Pickup -> Leg 2 -> Completion."""
    aid = None
    try:
        # Let the REPORTED alert stay visible on the Dispatcher map for 2.5s
        await asyncio.sleep(2.5)
        if not _sim_running:
            return

        async with AsyncSessionLocal() as db:
            inc = (await db.execute(
                text("""
                    SELECT emergency_id::text, status, nearest_node_id,
                           ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lon
                    FROM public.emergencies
                    WHERE emergency_id = CAST(:eid AS uuid)
                """),
                {"eid": eid},
            )).mappings().first()
            if not inc or inc["status"] not in ("RECEIVED", "REPORTED"):
                return

            # Pick nearest AVAILABLE ambulance via PostGIS KNN
            amb = (await db.execute(
                text("""
                    SELECT a.ambulance_id::text, a.call_sign,
                           (SELECT n.node_id FROM public.road_nodes n ORDER BY n.location <-> a.current_location LIMIT 1) AS amb_node_id
                    FROM public.ambulances a
                    WHERE a.status = 'AVAILABLE'
                    ORDER BY a.current_location <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                    LIMIT 1
                """),
                {"lon": inc["lon"], "lat": inc["lat"]},
            )).mappings().first()

            # Pick nearest Hubballi hospital with available beds
            hosp = (await db.execute(
                text("""
                    SELECT hospital_id::text, name, nearest_node_id,
                           ST_Y(location::geometry) AS hlat, ST_X(location::geometry) AS hlon
                    FROM public.hospitals
                    WHERE nearest_node_id IS NOT NULL
                    ORDER BY location::geometry <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                    LIMIT 1
                """),
                {"lon": inc["lon"], "lat": inc["lat"]},
            )).mappings().first()

            if not amb or not hosp or not inc["nearest_node_id"] or not amb["amb_node_id"]:
                return

            aid = amb["ambulance_id"]
            hid = hosp["hospital_id"]

            # Compute real pgRouting A* Leg 1 (Ambulance -> Emergency)
            leg1 = await compute_pgrouting_path(
                db, int(amb["amb_node_id"]), int(inc["nearest_node_id"]),
                algorithm="ASTAR", vehicle_mode="AMBULANCE",
            )
            if not leg1 or not leg1.get("coordinates"):
                return

            eta1 = max(15, int(round(leg1["total_duration_seconds"])))

            # Atomic assignment
            await db.execute(
                text("UPDATE public.ambulances SET status = 'ASSIGNED' WHERE ambulance_id = CAST(:aid AS uuid)"),
                {"aid": aid},
            )
            disp_id = (await db.execute(
                text("""
                    INSERT INTO public.dispatches (emergency_id, ambulance_id, hospital_id, status)
                    VALUES (CAST(:eid AS uuid), CAST(:aid AS uuid), CAST(:hid AS uuid), 'ACTIVE')
                    RETURNING dispatch_id::text
                """),
                {"eid": eid, "aid": aid, "hid": hid},
            )).scalar()
            await db.execute(
                text("""
                    UPDATE public.emergencies
                    SET assigned_ambulance_id = CAST(:aid AS uuid),
                        target_hospital_id = CAST(:hid AS uuid),
                        status = 'ASSIGNED', active_leg = 'LEG_1',
                        active_route_json = CAST(:rjson AS jsonb),
                        eta_seconds = :eta, remaining_distance_m = :rem,
                        dispatched_at = NOW()
                    WHERE emergency_id = CAST(:eid AS uuid)
                """),
                {"aid": aid, "hid": hid, "rjson": json.dumps(leg1), "eta": eta1, "rem": leg1["total_distance_meters"], "eid": eid},
            )
            await db.execute(
                text("""
                    INSERT INTO public.route_history (dispatch_id, algorithm, route_geom, distance_m, estimated_time_s, network_version)
                    VALUES (CAST(:did AS uuid), 'ASTAR', ST_SetSRID(ST_GeomFromGeoJSON(:gjson), 4326), :dm, :ts, 'hubballi-live-sim')
                """),
                {"did": disp_id, "gjson": json.dumps(leg1["geojson"]["geometry"]), "dm": leg1["total_distance_meters"], "ts": leg1["total_duration_seconds"]},
            )
            await db.execute(
                text("""
                    INSERT INTO public.emergency_events (emergency_id, old_status, new_status, changed_by_email, remarks)
                    VALUES (CAST(:eid AS uuid), 'REPORTED', 'ASSIGNED', 'auto-dispatch@iers.gov.in', :rem)
                """),
                {"eid": eid, "rem": f"Auto-dispatched {amb['call_sign']} -> {hosp['name']} ({eta1}s)"},
            )
            await db.commit()

        await _broadcast({
            "type": "DISPATCH_ASSIGNED",
            "incident_id": eid,
            "ambulance_id": aid,
            "hospital_id": hid,
            "eta_seconds": eta1,
            "route_coords": leg1["coordinates"],
            "leg": "LEG_1",
        })

        await asyncio.sleep(1.5)
        if not _sim_running:
            return

        # Transition to EN_ROUTE_TO_EMERGENCY & drive Leg 1
        async with AsyncSessionLocal() as db:
            await db.execute(text("UPDATE public.emergencies SET status = 'EN_ROUTE_TO_EMERGENCY' WHERE emergency_id = CAST(:eid AS uuid)"), {"eid": eid})
            await db.execute(text("UPDATE public.ambulances SET status = 'EN_ROUTE_TO_EMERGENCY' WHERE ambulance_id = CAST(:aid AS uuid)"), {"aid": aid})
            await db.commit()
        await _broadcast({"type": "CREW_STATUS_UPDATE", "incident_id": eid, "ambulance_id": aid, "status": "EN_ROUTE_TO_EMERGENCY"})

        await _drive_leg(eid, aid, leg1["coordinates"], leg1["total_distance_meters"], "LEG_1")
        if not _sim_running:
            return

        # Arrived at Emergency -> Patient Picked Up -> Compute Leg 2
        async with AsyncSessionLocal() as db:
            await db.execute(text("UPDATE public.emergencies SET status = 'ARRIVED_AT_EMERGENCY' WHERE emergency_id = CAST(:eid AS uuid)"), {"eid": eid})
            await db.execute(text("UPDATE public.ambulances SET status = 'ARRIVED_AT_EMERGENCY' WHERE ambulance_id = CAST(:aid AS uuid)"), {"aid": aid})
            await db.commit()
        await _broadcast({"type": "CREW_STATUS_UPDATE", "incident_id": eid, "ambulance_id": aid, "status": "ARRIVED_AT_EMERGENCY"})
        await asyncio.sleep(1.5)
        if not _sim_running:
            return

        async with AsyncSessionLocal() as db:
            leg2 = await compute_pgrouting_path(
                db, int(inc["nearest_node_id"]), int(hosp["nearest_node_id"]),
                algorithm="ASTAR", vehicle_mode="AMBULANCE",
            )
            if leg2 and leg2.get("coordinates"):
                eta2 = max(15, int(round(leg2["total_duration_seconds"])))
                await db.execute(
                    text("""
                        UPDATE public.emergencies
                        SET status = 'EN_ROUTE_TO_HOSPITAL', active_leg = 'LEG_2',
                            active_route_json = CAST(:rjson AS jsonb),
                            eta_seconds = :eta, remaining_distance_m = :rem
                        WHERE emergency_id = CAST(:eid AS uuid)
                    """),
                    {"rjson": json.dumps(leg2), "eta": eta2, "rem": leg2["total_distance_meters"], "eid": eid},
                )
                await db.execute(text("UPDATE public.ambulances SET status = 'EN_ROUTE_TO_HOSPITAL' WHERE ambulance_id = CAST(:aid AS uuid)"), {"aid": aid})
                await db.execute(
                    text("""
                        INSERT INTO public.emergency_events (emergency_id, old_status, new_status, changed_by_email, remarks)
                        VALUES (CAST(:eid AS uuid), 'ARRIVED_AT_EMERGENCY', 'EN_ROUTE_TO_HOSPITAL', 'auto-dispatch@iers.gov.in', :rem)
                    """),
                    {"eid": eid, "rem": f"Patient picked up; routing Leg 2 to {hosp['name']}"},
                )
                await db.commit()
            else:
                leg2 = None

        if leg2 and leg2.get("coordinates"):
            await _broadcast({
                "type": "CREW_STATUS_UPDATE",
                "incident_id": eid,
                "ambulance_id": aid,
                "status": "EN_ROUTE_TO_HOSPITAL",
                "route_coords": leg2["coordinates"],
                "leg": "LEG_2",
            })
            await _drive_leg(eid, aid, leg2["coordinates"], leg2["total_distance_meters"], "LEG_2")

        if not _sim_running:
            return

        # Arrived at Hospital -> Complete & Release Ambulance
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE public.emergencies SET status = 'COMPLETED', resolved_at = NOW(), eta_seconds = 0, remaining_distance_m = 0 WHERE emergency_id = CAST(:eid AS uuid)"),
                {"eid": eid},
            )
            await db.execute(
                text("UPDATE public.ambulances SET status = 'AVAILABLE' WHERE ambulance_id = CAST(:aid AS uuid)"),
                {"aid": aid},
            )
            await db.execute(
                text("UPDATE public.dispatches SET status = 'COMPLETED' WHERE emergency_id = CAST(:eid AS uuid)"),
                {"eid": eid},
            )
            await db.execute(
                text("""
                    INSERT INTO public.emergency_events (emergency_id, old_status, new_status, changed_by_email, remarks)
                    VALUES (CAST(:eid AS uuid), 'EN_ROUTE_TO_HOSPITAL', 'COMPLETED', 'auto-dispatch@iers.gov.in', 'Patient handed over at hospital; ambulance released to AVAILABLE')
                """),
                {"eid": eid},
            )
            await db.commit()

        _stats["completed_count"] += 1
        await _broadcast({"type": "INCIDENT_COMPLETED", "incident_id": eid, "ambulance_id": aid})
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[LiveSim] Mission {eid} error: {exc}")
    finally:
        _active_mission_tasks.pop(eid, None)


async def _spawn_one_emergency() -> Optional[str]:
    """Create a random emergency from a random Hubballi citizen at a random Hubballi road node."""
    async with AsyncSessionLocal() as db:
        citizen = (await db.execute(
            text("SELECT user_id::text, email, name AS full_name FROM public.users WHERE role = 'CITIZEN' ORDER BY random() LIMIT 1")
        )).mappings().first()
        node = (await db.execute(
            text("SELECT node_id, ST_Y(location) AS lat, ST_X(location) AS lon FROM public.road_nodes ORDER BY random() LIMIT 1")
        )).mappings().first()
        if not citizen or not node:
            return None

        priority, etype, title = random.choice(EMERGENCY_TEMPLATES)
        area = next((a for e, _n, a in SIM_CITIZENS if e == citizen["email"]), "Hubballi Central")
        code = f"SIM-{uuid.uuid4().hex[:6].upper()}"
        desc = f"{title} near {area} (Reported by {citizen['full_name']})"
        prio_score = 95 if priority == "P1" else (75 if priority == "P2" else 50)

        eid = (await db.execute(
            text("""
                INSERT INTO public.emergencies (
                    incident_code, reported_by, reported_by_email, location,
                    latitude, longitude, nearest_node_id, incident_type,
                    severity, priority_score, people_affected, description,
                    status, caller_name, caller_phone, needs_review, location_source
                ) VALUES (
                    :code, CAST(:uid AS uuid), :em,
                    ST_SetSRID(ST_MakePoint(CAST(:lon AS double precision), CAST(:lat AS double precision)), 4326),
                    :lat, :lon, :nid, :title,
                    :prio, :pscore, 1, :desc,
                    'RECEIVED', :cname, '112', FALSE, 'SIMULATED_CITIZEN'
                )
                RETURNING emergency_id::text
            """),
            {
                "code": code, "uid": citizen["user_id"], "em": citizen["email"],
                "lon": float(node["lon"]), "lat": float(node["lat"]),
                "nid": str(node["node_id"]), "title": title,
                "prio": priority, "pscore": prio_score, "desc": desc,
                "cname": citizen["full_name"],
            },
        )).scalar()

        await db.execute(
            text("""
                INSERT INTO public.emergency_events (emergency_id, old_status, new_status, changed_by, changed_by_email, remarks)
                VALUES (CAST(:eid AS uuid), NULL, 'RECEIVED', CAST(:uid AS uuid), :em, :rem)
            """),
            {"eid": eid, "uid": citizen["user_id"], "em": citizen["email"], "rem": desc},
        )
        await db.commit()

    _stats["spawned_count"] += 1
    await _broadcast({
        "type": "NEW_EMERGENCY",
        "incident_id": eid,
        "incident_code": code,
        "priority": priority,
        "description": desc,
        "lat": float(node["lat"]),
        "lon": float(node["lon"]),
    })
    return eid


async def _simulation_loop():
    global _sim_running
    await ensure_simulated_citizens()
    while _sim_running:
        try:
            # Prune finished tasks
            done_ids = [k for k, t in _active_mission_tasks.items() if t.done()]
            for k in done_ids:
                _active_mission_tasks.pop(k, None)

            if len(_active_mission_tasks) < _stats["max_concurrent"]:
                # Also pick up any unassigned SIM-* emergency left in RECEIVED
                orphan_eid = None
                async with AsyncSessionLocal() as db:
                    orphan_eid = (await db.execute(
                        text("""
                            SELECT emergency_id::text FROM public.emergencies
                            WHERE status = 'RECEIVED' AND incident_code LIKE 'SIM-%'
                            ORDER BY created_at ASC LIMIT 1
                        """)
                    )).scalar()
                if orphan_eid and orphan_eid not in _active_mission_tasks:
                    _active_mission_tasks[orphan_eid] = asyncio.create_task(_run_autonomous_incident(orphan_eid))
                else:
                    eid = await _spawn_one_emergency()
                    if eid:
                        _active_mission_tasks[eid] = asyncio.create_task(_run_autonomous_incident(eid))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[LiveSim] Loop error: {exc}")
        await asyncio.sleep(_stats["spawn_interval_s"])


async def start_simulation(max_concurrent: int = 4, spawn_interval_s: int = 8) -> Dict[str, Any]:
    global _sim_running, _sim_loop_task
    if _sim_running:
        return get_simulation_status()
    _sim_running = True
    _stats["started_at"] = datetime.utcnow().isoformat() + "Z"
    _stats["max_concurrent"] = max(1, min(8, max_concurrent))
    _stats["spawn_interval_s"] = max(4, min(30, spawn_interval_s))
    _sim_loop_task = asyncio.create_task(_simulation_loop())
    return get_simulation_status()


async def stop_simulation() -> Dict[str, Any]:
    global _sim_running, _sim_loop_task
    _sim_running = False
    if _sim_loop_task and not _sim_loop_task.done():
        _sim_loop_task.cancel()
    _sim_loop_task = None
    for t in list(_active_mission_tasks.values()):
        if not t.done():
            t.cancel()
    _active_mission_tasks.clear()
    return get_simulation_status()


def get_simulation_status() -> Dict[str, Any]:
    return {
        "running": _sim_running,
        "active_missions": len([t for t in _active_mission_tasks.values() if not t.done()]),
        "spawned_count": _stats["spawned_count"],
        "completed_count": _stats["completed_count"],
        "max_concurrent": _stats["max_concurrent"],
        "spawn_interval_s": _stats["spawn_interval_s"],
        "started_at": _stats["started_at"],
    }
