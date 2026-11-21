import enum
from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Enum, ForeignKey, Text
from sqlalchemy.orm import relationship
from geoalchemy2 import Geometry
from geoalchemy2.shape import to_shape
from app.core.database import Base

class VehicleType(str, enum.Enum):
    ALS = "ALS"          # Advanced Life Support (Large intensive care ambulance: requires wide roads, high bridge clearance)
    BLS = "BLS"          # Basic Life Support (Standard ambulance)
    RAPID_FLYER = "FLYER"# Rapid Response Motorcycle / Compact EV (Bypasses narrow alleys and tight bottlenecks)
    FLYER = "FLYER"      # Direct alias for RAPID_FLYER
    HELI = "HELI"        # Air ambulance (Point to point / Helipad)

class AmbulanceStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    DISPATCHED = "DISPATCHED"
    EN_ROUTE_INCIDENT = "EN_ROUTE_INCIDENT"
    AT_SCENE = "AT_SCENE"
    EN_ROUTE_HOSPITAL = "EN_ROUTE_HOSPITAL"
    OFF_DUTY = "OFF_DUTY"

class IncidentSeverity(str, enum.Enum):
    CRITICAL = "CRITICAL"    # Code Red - immediate ALS/Heli required
    URGENT = "URGENT"        # Code Yellow
    STANDARD = "STANDARD"    # Code Green

class IncidentStatus(str, enum.Enum):
    REPORTED = "REPORTED"
    ALLOCATED = "ALLOCATED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"

def _safe_geom_to_geojson(geom):
    if geom is None:
        return None
    try:
        return to_shape(geom).__geo_interface__
    except Exception:
        return None

class RoadNode(Base):
    __tablename__ = "road_network_nodes"
    __table_args__ = {"schema": "iers"}

    id = Column("node_id", String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=True)
    latitude = Column(Float, nullable=False, index=True)
    longitude = Column(Float, nullable=False, index=True)
    is_intersection = Column(Boolean, default=True)
    geom = Column(Geometry("POINT", srid=4326), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "is_intersection": self.is_intersection,
            "geom_geojson": _safe_geom_to_geojson(self.geom)
        }

class RoadEdge(Base):
    __tablename__ = "road_network_edges"
    __table_args__ = {"schema": "iers"}

    id = Column("edge_id", String(64), primary_key=True, index=True)
    source_id = Column(String(64), ForeignKey("iers.road_network_nodes.node_id"), nullable=False, index=True)
    target_id = Column(String(64), ForeignKey("iers.road_network_nodes.node_id"), nullable=False, index=True)
    source_vertex_id = Column(Integer, nullable=True)
    target_vertex_id = Column(Integer, nullable=True)
    road_name = Column(String(128), nullable=False)
    distance_meters = Column(Float, nullable=False)
    speed_limit_kmh = Column(Float, default=40.0)
    current_traffic_factor = Column(Float, default=1.0)
    max_height_meters = Column(Float, default=4.5)
    max_weight_tons = Column(Float, default=20.0)
    width_meters = Column(Float, default=6.0)
    is_one_way = Column(Boolean, default=False)
    is_emergency_lane_active = Column("is_emergency_corridor", Boolean, default=False)
    geom = Column(Geometry("LINESTRING", srid=4326), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "road_name": self.road_name,
            "distance_meters": self.distance_meters,
            "speed_limit_kmh": self.speed_limit_kmh,
            "current_traffic_factor": self.current_traffic_factor,
            "max_height_meters": self.max_height_meters,
            "max_weight_tons": self.max_weight_tons,
            "width_meters": self.width_meters,
            "is_one_way": self.is_one_way,
            "is_emergency_lane_active": self.is_emergency_lane_active,
            "geom_geojson": _safe_geom_to_geojson(self.geom)
        }

class Hospital(Base):
    __tablename__ = "hospitals"
    __table_args__ = {"schema": "iers"}

    id = Column("hospital_id", String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    nearest_node_id = Column(String(64), nullable=True)
    total_beds = Column(Integer, default=50)
    icu_available = Column(Integer, default=10)
    trauma_level = Column(Integer, default=1)
    has_helipad = Column(Boolean, default=False)
    geom = Column(Geometry("POINT", srid=4326), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "nearest_node_id": self.nearest_node_id,
            "total_beds": self.total_beds,
            "icu_available": self.icu_available,
            "trauma_level": self.trauma_level,
            "has_helipad": self.has_helipad,
            "geom_geojson": _safe_geom_to_geojson(self.geom)
        }

class Ambulance(Base):
    __tablename__ = "ambulances"
    __table_args__ = {"schema": "iers"}

    id = Column("ambulance_id", String(64), primary_key=True, index=True)
    call_sign = Column(String(64), nullable=False, unique=True)
    vehicle_type = Column(
        Enum(VehicleType, values_callable=lambda x: [e.value for e in x], native_enum=False),
        default=VehicleType.BLS,
        nullable=False
    )
    status = Column(
        Enum(AmbulanceStatus, values_callable=lambda x: [e.value for e in x], native_enum=False),
        default=AmbulanceStatus.AVAILABLE,
        nullable=False
    )
    height_meters = Column(Float, default=2.4)
    weight_tons = Column(Float, default=3.5)
    width_meters = Column(Float, default=2.2)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    current_node_id = Column(String(64), nullable=True)
    assigned_hospital_id = Column(String(64), nullable=True)
    driver_name = Column(String(128), default="Paramedic Crew")
    fuel_percent = Column(Float, default=100.0)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    geom = Column(Geometry("POINT", srid=4326), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "call_sign": self.call_sign,
            "vehicle_type": self.vehicle_type.value if hasattr(self.vehicle_type, "value") else str(self.vehicle_type),
            "status": self.status.value if hasattr(self.status, "value") else str(self.status),
            "height_meters": self.height_meters,
            "weight_tons": self.weight_tons,
            "width_meters": self.width_meters,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "current_node_id": self.current_node_id,
            "assigned_hospital_id": self.assigned_hospital_id,
            "driver_name": self.driver_name,
            "fuel_percent": self.fuel_percent,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "geom_geojson": _safe_geom_to_geojson(self.geom)
        }

class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = {"schema": "iers"}

    id = Column("incident_id", String(64), primary_key=True, index=True)
    caller_name = Column(String(128), default="Emergency Caller")
    caller_phone = Column(String(32), default="911 / 112")
    incident_type = Column(String(64), default="Cardiac Arrest / Severe Trauma")
    severity = Column(
        Enum(IncidentSeverity, values_callable=lambda x: [e.value for e in x], native_enum=False),
        default=IncidentSeverity.CRITICAL,
        nullable=False
    )
    status = Column(
        Enum(IncidentStatus, values_callable=lambda x: [e.value for e in x], native_enum=False),
        default=IncidentStatus.REPORTED,
        nullable=False
    )
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    nearest_node_id = Column(String(64), nullable=True)
    assigned_ambulance_id = Column(String(64), nullable=True)
    target_hospital_id = Column(String(64), nullable=True)
    reported_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)
    geom = Column(Geometry("POINT", srid=4326), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "caller_name": self.caller_name,
            "caller_phone": self.caller_phone,
            "incident_type": self.incident_type,
            "severity": self.severity.value if hasattr(self.severity, "value") else str(self.severity),
            "status": self.status.value if hasattr(self.status, "value") else str(self.status),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "nearest_node_id": self.nearest_node_id,
            "assigned_ambulance_id": self.assigned_ambulance_id,
            "target_hospital_id": self.target_hospital_id,
            "reported_at": self.reported_at.isoformat() if self.reported_at else None,
            "dispatched_at": self.dispatched_at.isoformat() if self.dispatched_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "notes": self.notes,
            "geom_geojson": _safe_geom_to_geojson(self.geom)
        }
