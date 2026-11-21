-- ============================================================================
-- 08_validation.sql
-- Intelligent Emergency Response System (IERS)
-- Comprehensive GIS Data Quality, SRID, Validity, & Integrity Audit
-- ============================================================================

SET search_path TO emergency, public;

-- 1. Check SRID Consistency (Must be 4326 for all layers)
SELECT 
    f_table_name,
    f_geometry_column,
    srid,
    type
FROM geometry_columns
WHERE f_table_schema = 'emergency';

-- 2. Check Geometry Validity (ST_IsValid = TRUE, ST_IsEmpty = FALSE)
SELECT 
    'road_network_nodes' AS layer,
    COUNT(*) AS total_features,
    COUNT(*) FILTER (WHERE ST_IsValid(geom) = FALSE) AS invalid_geometries,
    COUNT(*) FILTER (WHERE ST_IsEmpty(geom) = TRUE) AS empty_geometries
FROM emergency.road_network_nodes
UNION ALL
SELECT 
    'road_network_edges' AS layer,
    COUNT(*) AS total_features,
    COUNT(*) FILTER (WHERE ST_IsValid(geom) = FALSE) AS invalid_geometries,
    COUNT(*) FILTER (WHERE ST_IsEmpty(geom) = TRUE) AS empty_geometries
FROM emergency.road_network_edges;

-- 3. Check Bounding Box Extents of imported network
SELECT 
    ST_Extent(geom) AS road_network_bounding_box,
    ST_XMin(ST_Extent(geom)) AS min_lon,
    ST_XMax(ST_Extent(geom)) AS max_lon,
    ST_YMin(ST_Extent(geom)) AS min_lat,
    ST_YMax(ST_Extent(geom)) AS max_lat
FROM emergency.road_network_edges;

-- 4. Test Sample Shortest-Path Route Execution
SELECT 
    seq,
    node_id,
    road_name,
    ROUND(cost_seconds::numeric, 2) AS duration_seconds,
    ROUND(distance_meters::numeric, 1) AS distance_meters
FROM emergency.calculate_constrained_route('NODE_567', 'NODE_473', 2.5, 3.5, 2.2);
