-- ============================================================================
-- 06_network_analysis.sql
-- Intelligent Emergency Response System (IERS)
-- Spatial Network Analysis, Buffer Zones, & Nearest Facility Queries
-- ============================================================================

SET search_path TO emergency, public;

-- Query 1: Nearest Road Node to any Incident Coordinate (KNN Distance Operator <->)
-- Finds closest network intersection within sub-millisecond execution
CREATE OR REPLACE FUNCTION emergency.find_nearest_node(
    p_lon DOUBLE PRECISION,
    p_lat DOUBLE PRECISION
) 
RETURNS TABLE (
    node_id VARCHAR(64),
    name VARCHAR(128),
    distance_meters DOUBLE PRECISION
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        n.node_id,
        n.name,
        ST_Distance(n.geog, ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326)::geography) AS distance_meters
    FROM emergency.road_network_nodes n
    ORDER BY n.geom <-> ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326)
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE;

-- Query 2: 500-Meter Rapid Response Hospital Coverage Buffers
CREATE OR REPLACE VIEW emergency.v_hospital_coverage_500m AS
SELECT 
    hospital_id,
    name,
    icu_available,
    trauma_level,
    ST_Buffer(geom::geography, 500)::geometry AS coverage_geom
FROM emergency.hospitals;

-- Query 3: Road Connectivity Density (Degree Centrality per Node)
CREATE OR REPLACE VIEW emergency.v_node_centrality AS
SELECT 
    n.node_id,
    n.name,
    COUNT(e.edge_id) AS connected_edges,
    n.geom
FROM emergency.road_network_nodes n
JOIN emergency.road_network_edges e 
    ON n.node_id = e.source_id OR n.node_id = e.target_id
GROUP BY n.node_id, n.name, n.geom
ORDER BY connected_edges DESC;
