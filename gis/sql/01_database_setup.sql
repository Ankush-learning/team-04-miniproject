-- ============================================================================
-- 01_database_setup.sql
-- Intelligent Emergency Response System (IERS)
-- Production PostGIS & pgRouting Extension Initialization
-- ============================================================================

-- Create database if not exists (Execute as postgres superuser)
-- CREATE DATABASE emergency_db WITH ENCODING 'UTF8';

-- Connect to emergency_db
\connect emergency_db;

-- 1. Enable Core Spatial Extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- 2. Enable Network Routing Extensions (pgRouting)
CREATE EXTENSION IF NOT EXISTS pgrouting;

-- 3. Utility Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Verify spatial capabilities and versions
SELECT 
    postgis_full_version() AS postgis_version,
    pgr_version() AS pgrouting_version;
