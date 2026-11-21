-- ============================================================================
-- 07_emergency_routing.sql
-- Intelligent Emergency Response System (IERS)
-- Constrained Dijkstra Routing via pgRouting (pgr_dijkstra)
-- Accounts for Vehicle Height, Width, Bridge Weight, & Live Congestion
-- ============================================================================

SET search_path TO emergency, public;

CREATE OR REPLACE FUNCTION emergency.calculate_constrained_route(
    p_start_node VARCHAR(64),
    p_end_node VARCHAR(64),
    p_veh_height DOUBLE PRECISION DEFAULT 2.5,
    p_veh_weight DOUBLE PRECISION DEFAULT 4.0,
    p_veh_width DOUBLE PRECISION DEFAULT 2.2
)
RETURNS TABLE (
    seq INTEGER,
    node_id VARCHAR(64),
    edge_id VARCHAR(64),
    road_name VARCHAR(128),
    cost_seconds DOUBLE PRECISION,
    distance_meters DOUBLE PRECISION,
    geom GEOMETRY
) AS $$
DECLARE
    v_start_id INTEGER;
    v_end_id INTEGER;
    v_sql TEXT;
BEGIN
    -- Resolve integer vertex IDs for pgRouting
    SELECT vertex_id INTO v_start_id FROM emergency.vertices_map WHERE node_id = p_start_node;
    SELECT vertex_id INTO v_end_id FROM emergency.vertices_map WHERE node_id = p_end_node;

    IF v_start_id IS NULL OR v_end_id IS NULL THEN
        RAISE EXCEPTION 'Start or End node not found in topology network';
    END IF;

    -- Dynamic SQL filtering edges that violate clearance limits
    v_sql := format(
        'SELECT 
            pgr_id AS id, 
            source_vertex_id AS source, 
            target_vertex_id AS target, 
            cost_seconds AS cost, 
            reverse_cost_seconds AS reverse_cost 
         FROM emergency.road_network_edges
         WHERE max_height_meters >= %s 
           AND max_weight_tons >= %s 
           AND width_meters >= %s',
        p_veh_height, p_veh_weight, p_veh_width
    );

    RETURN QUERY
    SELECT 
        d.seq,
        vm.node_id,
        e.edge_id,
        e.road_name,
        d.cost AS cost_seconds,
        COALESCE(e.distance_meters, 0.0) AS distance_meters,
        COALESCE(e.geom, vm.geom) AS geom
    FROM pgr_dijkstra(v_sql, v_start_id, v_end_id, directed := true) d
    LEFT JOIN emergency.vertices_map vm ON d.node = vm.vertex_id
    LEFT JOIN emergency.road_network_edges e ON d.edge = e.pgr_id
    ORDER BY d.seq;
END;
$$ LANGUAGE plpgsql STABLE;
