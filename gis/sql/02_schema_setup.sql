-- ============================================================================
-- 02_schema_setup.sql
-- Intelligent Emergency Response System (IERS)
-- Relational Schemas & Geometric Definitions (SRID 4326 - WGS 84)
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS emergency;
SET search_path TO emergency, public;

-- Drop existing views or foreign key dependents safely
DROP TABLE IF EXISTS emergency.incidents CASCADE;
DROP TABLE IF EXISTS emergency.ambulances CASCADE;
DROP TABLE IF EXISTS emergency.hospitals CASCADE;
DROP TABLE IF EXISTS emergency.road_network_edges CASCADE;
DROP TABLE IF EXISTS emergency.road_network_nodes CASCADE;

-- 1. Road Network Vertices (Nodes)
CREATE TABLE emergency.road_network_nodes (
    node_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128),
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(Point, 4326) NOT NULL,
    geog GEOGRAPHY(Point, 4326) GENERATED ALWAYS AS (geom::geography) STORED,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Road Network Edges (Segments with Clearance & Vehicle Constraints)
CREATE TABLE emergency.road_network_edges (
    edge_id VARCHAR(64) PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL REFERENCES emergency.road_network_nodes(node_id),
    target_id VARCHAR(64) NOT NULL REFERENCES emergency.road_network_nodes(node_id),
    source_vertex_id INTEGER, -- Integer ID required for pgRouting pgr_dijkstra
    target_vertex_id INTEGER,
    road_name VARCHAR(128) NOT NULL,
    distance_meters DOUBLE PRECISION NOT NULL,
    speed_limit_kmh DOUBLE PRECISION DEFAULT 40.0,
    current_traffic_factor DOUBLE PRECISION DEFAULT 1.0,
    max_height_meters DOUBLE PRECISION DEFAULT 4.5, -- Bridge/archway clearance
    max_weight_tons DOUBLE PRECISION DEFAULT 25.0,  -- Bridge rating
    width_meters DOUBLE PRECISION DEFAULT 6.0,      -- Alley/street width
    is_one_way BOOLEAN DEFAULT FALSE,
    is_emergency_corridor BOOLEAN DEFAULT FALSE,
    geom GEOMETRY(LineString, 4326) NOT NULL,
    geog GEOGRAPHY(LineString, 4326) GENERATED ALWAYS AS (geom::geography) STORED,
    
    -- Routing cost: Travel time in seconds with dynamic congestion & emergency lane dampening
    cost_seconds DOUBLE PRECISION GENERATED ALWAYS AS (
        distance_meters / GREATEST(
            ((speed_limit_kmh / 3.6) / 
             CASE 
                WHEN is_emergency_corridor THEN (1.0 + (current_traffic_factor - 1.0) * 0.4)
                ELSE current_traffic_factor
             END), 0.5)
    ) STORED,
    reverse_cost_seconds DOUBLE PRECISION
);

-- 3. Hospitals & Trauma Hubs
CREATE TABLE emergency.hospitals (
    hospital_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    nearest_node_id VARCHAR(64) REFERENCES emergency.road_network_nodes(node_id),
    total_beds INTEGER DEFAULT 100,
    icu_available INTEGER DEFAULT 15,
    trauma_level INTEGER DEFAULT 1,
    has_helipad BOOLEAN DEFAULT TRUE,
    geom GEOMETRY(Point, 4326) NOT NULL
);

-- 4. Ambulances Fleet
CREATE TABLE emergency.ambulances (
    ambulance_id VARCHAR(64) PRIMARY KEY,
    call_sign VARCHAR(64) UNIQUE NOT NULL,
    vehicle_type VARCHAR(32) NOT NULL, -- ALS, BLS, FLYER, HELI
    status VARCHAR(32) DEFAULT 'AVAILABLE',
    height_meters DOUBLE PRECISION DEFAULT 2.8,
    weight_tons DOUBLE PRECISION DEFAULT 4.0,
    width_meters DOUBLE PRECISION DEFAULT 2.4,
    current_node_id VARCHAR(64) REFERENCES emergency.road_network_nodes(node_id),
    driver_name VARCHAR(128),
    geom GEOMETRY(Point, 4326) NOT NULL
);

-- 5. Incidents
CREATE TABLE emergency.incidents (
    incident_id VARCHAR(64) PRIMARY KEY,
    caller_name VARCHAR(128),
    incident_type VARCHAR(128),
    severity VARCHAR(32) NOT NULL, -- CRITICAL, URGENT, STANDARD
    status VARCHAR(32) DEFAULT 'REPORTED',
    nearest_node_id VARCHAR(64) REFERENCES emergency.road_network_nodes(node_id),
    assigned_ambulance_id VARCHAR(64) REFERENCES emergency.ambulances(ambulance_id),
    target_hospital_id VARCHAR(64) REFERENCES emergency.hospitals(hospital_id),
    reported_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP WITH TIME ZONE,
    geom GEOMETRY(Point, 4326) NOT NULL
);
