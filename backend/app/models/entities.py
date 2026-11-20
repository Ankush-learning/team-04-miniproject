import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Enum, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.core.database import Base

class VehicleType(str, enum.Enum):
    ALS = "ALS"          # Advanced Life Support (Large intensive care ambulance: requires wide roads, high bridge clearance)
    BLS = "BLS"          # Basic Life Support (Standard ambulance)
    RAPID_FLYER = "FLYER"# Rapid Response Motorcycle / Compact EV (Bypasses narrow alleys and tight bottlenecks)
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

class RoadNode(Base):
    __tablename__ = "road_nodes"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=True)
    latitude = Column(Float, nullable=False, index=True)
    longitude = Column(Float, nullable=False, index=True)
    is_intersection = Column(Boolean, default=True)

class RoadEdge(Base):
    __tablename__ = "road_edges"

    id = Column(String(64), primary_key=True, index=True)
    source_id = Column(String(64), ForeignKey("road_nodes.id"), nullable=False, index=True)
    target_id = Column(String(64), ForeignKey("road_nodes.id"), nullable=False, index=True)
    road_name = Column(String(128), nullable=False)
    distance_meters = Column(Float, nullable=False)
    speed_limit_kmh = Column(Float, default=50.0)
    current_traffic_factor = Column(Float, default=1.0) # 1.0 = Free flow, 3.0+ = heavy congestion
    
    # Vehicle physical constraints
    max_height_meters = Column(Float, default=4.5)     # Clearance height (ALS ambulances need >= 3.0m)
    max_weight_tons = Column(Float, default=20.0)       # Bridge weight rating
    width_meters = Column(Float, default=7.0)           # Road width (Narrow alleys < 2.8m disqualify ALS)
    is_one_way = Column(Boolean, default=False)
    is_emergency_lane_active = Column(Boolean, default=False)

class Hospital(Base):
    __tablename__ = "hospitals"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    nearest_node_id = Column(String(64), ForeignKey("road_nodes.id"), nullable=False)
    total_beds = Column(Integer, default=50)
    icu_available = Column(Integer, default=10)
    trauma_level = Column(Integer, default=1) # Level 1 = highest tier trauma center
    has_helipad = Column(Boolean, default=False)

class Ambulance(Base):
    __tablename__ = "ambulances"

    id = Column(String(64), primary_key=True, index=True)
    call_sign = Column(String(64), nullable=False, unique=True)
    vehicle_type = Column(Enum(VehicleType), default=VehicleType.BLS, nullable=False)
    status = Column(Enum(AmbulanceStatus), default=AmbulanceStatus.AVAILABLE, nullable=False)
    
    # Physical specifications for clearance routing
    height_meters = Column(Float, default=2.4)
    weight_tons = Column(Float, default=3.5)
    width_meters = Column(Float, default=2.2)
    
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    current_node_id = Column(String(64), ForeignKey("road_nodes.id"), nullable=True)
    assigned_hospital_id = Column(String(64), ForeignKey("hospitals.id"), nullable=True)
    
    driver_name = Column(String(128), default="Paramedic Crew")
    fuel_percent = Column(Float, default=100.0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Incident(Base):
    __tablename__ = "incidents"

    id = Column(String(64), primary_key=True, index=True)
    caller_name = Column(String(128), default="Emergency Caller")
    caller_phone = Column(String(32), default="911 / 112")
    incident_type = Column(String(64), default="Cardiac Arrest / Severe Trauma")
    severity = Column(Enum(IncidentSeverity), default=IncidentSeverity.CRITICAL, nullable=False)
    status = Column(Enum(IncidentStatus), default=IncidentStatus.REPORTED, nullable=False)
    
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    nearest_node_id = Column(String(64), ForeignKey("road_nodes.id"), nullable=False)
    assigned_ambulance_id = Column(String(64), ForeignKey("ambulances.id"), nullable=True)
    target_hospital_id = Column(String(64), ForeignKey("hospitals.id"), nullable=True)
    
    reported_at = Column(DateTime, default=datetime.utcnow)
    dispatched_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
