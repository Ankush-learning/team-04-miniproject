-- ============================================================================
-- 05_spatial_indexes.sql
-- Intelligent Emergency Response System (IERS)
-- Spatial GiST Indexing & Performance Optimization
-- ============================================================================

SET search_path TO emergency, public;

-- 1. GiST Indexes on Geometry columns for millisecond bounding-box filtering
CREATE INDEX IF NOT EXISTS idx_road_nodes_geom 
    ON emergency.road_network_nodes USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_road_edges_geom 
    ON emergency.road_network_edges USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_hospitals_geom 
    ON emergency.hospitals USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_ambulances_geom 
    ON emergency.ambulances USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_incidents_geom 
    ON emergency.incidents USING GIST (geom);

-- 2. B-Tree Indexes on Foreign Keys and Routing Vertex IDs
CREATE INDEX IF NOT EXISTS idx_edges_source_target_vertex 
    ON emergency.road_network_edges (source_vertex_id, target_vertex_id);

CREATE INDEX IF NOT EXISTS idx_ambulances_status 
    ON emergency.ambulances (status);

CREATE INDEX IF NOT EXISTS idx_incidents_status_severity 
    ON emergency.incidents (status, severity);

-- 3. Run VACUUM ANALYZE to update optimizer table statistics
ANALYZE emergency.road_network_nodes;
ANALYZE emergency.road_network_edges;
ANALYZE emergency.hospitals;
ANALYZE emergency.ambulances;
ANALYZE emergency.incidents;
