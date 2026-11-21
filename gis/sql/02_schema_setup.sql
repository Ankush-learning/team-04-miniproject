-- ============================================================================
-- 02_schema_setup.sql
-- IERS — Table schema inside iers_db.iers schema
-- ============================================================================
SET search_path TO iers, public;

-- Road network nodes (intersections / waypoints)
CREATE TABLE IF NOT EXISTS iers.road_network_nodes (
    node_id         TEXT PRIMARY KEY,
    name            TEXT,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    geom            GEOMETRY(Point, 4326)
);

-- Road network edges (segments with vehicle clearance attributes)
CREATE TABLE IF NOT EXISTS iers.road_network_edges (
    edge_id                 TEXT PRIMARY KEY,
    source_id               TEXT NOT NULL REFERENCES iers.road_network_nodes(node_id),
    target_id               TEXT NOT NULL REFERENCES iers.road_network_nodes(node_id),
    source_vertex_id        INTEGER,
    target_vertex_id        INTEGER,
    road_name               TEXT,
    distance_meters         DOUBLE PRECISION DEFAULT 100.0,
    speed_limit_kmh         DOUBLE PRECISION DEFAULT 40.0,
    current_traffic_factor  DOUBLE PRECISION DEFAULT 1.0,
    max_height_meters       DOUBLE PRECISION DEFAULT 4.5,
    max_weight_tons         DOUBLE PRECISION DEFAULT 20.0,
    width_meters            DOUBLE PRECISION DEFAULT 6.0,
    is_one_way              BOOLEAN DEFAULT FALSE,
    is_emergency_corridor   BOOLEAN DEFAULT FALSE,
    geom                    GEOMETRY(LineString, 4326),
    geog                    GEOGRAPHY(LineString, 4326),
    cost_seconds            DOUBLE PRECISION GENERATED ALWAYS AS
                                (distance_meters / NULLIF(speed_limit_kmh * current_traffic_factor, 0) * 3.6)
                                STORED,
    reverse_cost_seconds    DOUBLE PRECISION GENERATED ALWAYS AS
                                (CASE WHEN is_one_way THEN 1e9
                                 ELSE distance_meters / NULLIF(speed_limit_kmh * current_traffic_factor, 0) * 3.6
                                 END) STORED,
    pgr_id                  SERIAL UNIQUE
);

-- Hospitals
CREATE TABLE IF NOT EXISTS iers.hospitals (
    hospital_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    total_beds      INTEGER DEFAULT 0,
    icu_available   INTEGER DEFAULT 0,
    trauma_level    INTEGER DEFAULT 2,
    has_helipad     BOOLEAN DEFAULT FALSE,
    geom            GEOMETRY(Point, 4326)
);

-- Ambulances
CREATE TABLE IF NOT EXISTS iers.ambulances (
    ambulance_id    TEXT PRIMARY KEY,
    call_sign       TEXT,
    vehicle_type    TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    status          TEXT DEFAULT 'AVAILABLE',
    current_node_id TEXT,
    geom            GEOMETRY(Point, 4326)
);

-- Incidents
CREATE TABLE IF NOT EXISTS iers.incidents (
    incident_id     TEXT PRIMARY KEY,
    incident_type   TEXT,
    severity        TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    status          TEXT DEFAULT 'OPEN',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    geom            GEOMETRY(Point, 4326)
);

-- Vertices map for topology
CREATE TABLE IF NOT EXISTS iers.vertices_map (
    vertex_id       SERIAL PRIMARY KEY,
    node_id         TEXT UNIQUE REFERENCES iers.road_network_nodes(node_id),
    geom            GEOMETRY(Point, 4326)
);

SELECT tablename FROM pg_tables WHERE schemaname = 'iers' ORDER BY tablename;
