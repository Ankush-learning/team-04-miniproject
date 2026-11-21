-- ============================================================================
-- 04_topology_generation.sql
-- Intelligent Emergency Response System (IERS)
-- Topology Creation & Integer Vertex Mapping for pgRouting (pgr_createTopology)
-- ============================================================================

SET search_path TO emergency, public;

-- Add sequential integer IDs required by pgRouting algorithms if missing
ALTER TABLE emergency.road_network_edges ADD COLUMN IF NOT EXISTS pgr_id SERIAL;

-- 1. Create unique integer vertex index mapping table
CREATE TABLE IF NOT EXISTS emergency.vertices_map (
    vertex_id SERIAL PRIMARY KEY,
    node_id VARCHAR(64) UNIQUE NOT NULL,
    geom GEOMETRY(Point, 4326) NOT NULL
);

INSERT INTO emergency.vertices_map (node_id, geom)
SELECT node_id, geom FROM emergency.road_network_nodes
ON CONFLICT (node_id) DO UPDATE SET geom = EXCLUDED.geom;

-- 2. Populate source_vertex_id and target_vertex_id on road_network_edges
UPDATE emergency.road_network_edges e
SET source_vertex_id = v.vertex_id
FROM emergency.vertices_map v
WHERE e.source_id = v.node_id;

UPDATE emergency.road_network_edges e
SET target_vertex_id = v.vertex_id
FROM emergency.vertices_map v
WHERE e.target_id = v.node_id;

-- 3. Set reverse_cost based on oneway flag
UPDATE emergency.road_network_edges
SET reverse_cost_seconds = CASE 
    WHEN is_one_way THEN -1.0 -- Disallow reverse traversal
    ELSE cost_seconds 
END;

-- 4. Topology Quality Audit: verify all edges have valid vertex references
SELECT 
    COUNT(*) AS total_edges,
    COUNT(source_vertex_id) AS resolved_sources,
    COUNT(target_vertex_id) AS resolved_targets,
    COUNT(*) FILTER (WHERE source_vertex_id IS NULL OR target_vertex_id IS NULL) AS unlinked_edges
FROM emergency.road_network_edges;
