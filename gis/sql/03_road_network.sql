-- ============================================================================
-- 03_road_network.sql
-- Intelligent Emergency Response System (IERS)
-- Population of Real GIS Road Network and Assets (From GeoPackage / JSON source)
-- ============================================================================

SET search_path TO emergency, public;

-- Sample representative seed verifying DDL and geometry creation
-- Complete population is automatically synced via gis/scripts/import_gis_network.py

-- Insert Baseline Emergency Care Facilities
INSERT INTO emergency.hospitals (hospital_id, name, nearest_node_id, total_beds, icu_available, trauma_level, has_helipad, geom)
VALUES 
('H_01', 'KIMS Medical Center & Level 1 Trauma Hub', 'NODE_473', 180, 22, 1, TRUE, ST_SetSRID(ST_MakePoint(75.123161, 15.369641), 4326)),
('H_02', 'Vidyanagar Emergency Care & Cardiac Center', 'NODE_964', 90, 8, 2, FALSE, ST_SetSRID(ST_MakePoint(75.123812, 15.365116), 4326))
ON CONFLICT (hospital_id) DO UPDATE SET
    name = EXCLUDED.name,
    icu_available = EXCLUDED.icu_available,
    geom = EXCLUDED.geom;

-- Insert Fleet Assets
INSERT INTO emergency.ambulances (ambulance_id, call_sign, vehicle_type, status, height_meters, weight_tons, width_meters, current_node_id, driver_name, geom)
VALUES
('AMB_01', 'PARAMEDIC-ALS-01', 'ALS', 'AVAILABLE', 3.2, 5.2, 2.6, 'NODE_567', 'Harris Whitaker (EMT-P)', ST_SetSRID(ST_MakePoint(75.121066, 15.368637), 4326)),
('AMB_02', 'RAPID-FLYER-02', 'FLYER', 'AVAILABLE', 1.8, 0.4, 1.1, 'NODE_964', 'Sofia Lind', ST_SetSRID(ST_MakePoint(75.123812, 15.365116), 4326)),
('AMB_03', 'RESCUE-BLS-03', 'BLS', 'AVAILABLE', 2.5, 3.6, 2.3, 'NODE_473', 'Lars Nyström', ST_SetSRID(ST_MakePoint(75.123161, 15.369641), 4326))
ON CONFLICT (ambulance_id) DO UPDATE SET
    status = EXCLUDED.status,
    geom = EXCLUDED.geom;
