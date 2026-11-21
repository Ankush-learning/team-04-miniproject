# Intelligent Emergency Response System (IERS) — GIS & PostGIS Layer

This documentation outlines the spatial database architecture, PostGIS + pgRouting integration, SQL analysis scripts, and QGIS demonstration setup for academic presentation and evaluation.

---

## 1. System Architecture

The IERS GIS subsystem is structured into three layers:

```
┌────────────────────────────────────────────────────────┐
│           QGIS 3.44+ & SaaS Operations Web UI          │
│   (Carto / Voyager, Dynamic Polylines, Layer Controls)  │
└──────────────────────────┬─────────────────────────────┘
                           │ GeoJSON / REST / WebSocket / QGIS Provider
┌──────────────────────────▼─────────────────────────────┐
│               FastAPI Spatial Backend                  │
│  (RoutingEngine, Concurrency Lock, Traffic Simulator)  │
└──────────────────────────┬─────────────────────────────┘
                           │ SQL / PostGIS / pgRouting
┌──────────────────────────▼─────────────────────────────┐
│          PostgreSQL 18 + PostGIS + pgRouting           │
│  (1,282 Nodes, 2,267 Segments, GiST Indexes, Dijkstra) │
└────────────────────────────────────────────────────────┘
```

---

## 2. Spatial Schema & Geometry Specification

- **Spatial Reference System (SRID)**: `EPSG:4326` (WGS 84 coordinate system).
- **Network Dimensions**:
  - Vertices (`emergency.road_network_nodes`): 1,282 unique topological intersections.
  - Edges (`emergency.road_network_edges`): 2,267 road segments with clearance parameters (`max_height_meters`, `max_weight_tons`, `width_meters`).
- **Dynamic Cost Model**:
  $$\text{Cost (seconds)} = \frac{\text{Distance (m)}}{\text{Speed Limit (m/s)} \div \text{Effective Traffic}}$$
  - Emergency Green Corridors reduce traffic delay penalties by **$60\%$**.

---

## 3. SQL Pipeline Execution Guide

Execute the modular SQL scripts located in `gis/sql/` in sequence:

```bash
# 1. Database & Extension Initialization
psql -U postgres -d emergency_db -f gis/sql/01_database_setup.sql

# 2. Schema DDL & Geometric Constraints
psql -U postgres -d emergency_db -f gis/sql/02_schema_setup.sql

# 3. Baseline Asset Population (Hospitals & Fleet)
psql -U postgres -d emergency_db -f gis/sql/03_road_network.sql

# 4. Topology Generation & pgRouting Vertex Indexing
psql -U postgres -d emergency_db -f gis/sql/04_topology_generation.sql

# 5. Spatial GiST & B-Tree Indexes
psql -U postgres -d emergency_db -f gis/sql/05_spatial_indexes.sql

# 6. Spatial Proximity & Coverage Buffer Functions
psql -U postgres -d emergency_db -f gis/sql/06_network_analysis.sql

# 7. Constrained Dijkstra Routing (Physical Clearance)
psql -U postgres -d emergency_db -f gis/sql/07_emergency_routing.sql

# 8. Data Integrity & Validation Audit
psql -U postgres -d emergency_db -f gis/sql/08_validation.sql
```

---

## 4. Live Professor Demonstration Sequence

Run `gis/sql/live_professor_demo.sql` directly:

```bash
psql -U postgres -d emergency_db -f gis/sql/live_professor_demo.sql
```

### Demonstration Steps:
1. **Extension Capabilities**: Confirms active PostGIS and pgRouting versions.
2. **Network Scale**: Reports exact node and segment counts.
3. **Sub-Millisecond KNN Proximity**: Executes `ST_Distance` using `<->` spatial KNN index operator.
4. **Physical Clearance Validation**: Runs constrained routing for ALS ambulance (`height=3.2m`, `width=2.6m`), confirming rejection of undersized alleys and low bridges.
5. **Emergency Green Corridor**: Demonstrates travel-time reduction upon priority lane activation.

---

## 5. QGIS Project Setup (`gis/qgis/`)

1. Open **QGIS** (`C:\Program Files\QGIS 3.44.15\bin\qgis-ltr-bin.exe`).
2. Open `gis/qgis/Intelligent_Emergency_Response.qgz`.
3. Pre-configured layers include:
   - **Road Network**: Categorized by highway class (`road_network_style.qml`).
   - **Campus Boundaries**: Translucent polygon overlay (`kle_campus.gpkg`).
   - **Emergency Route Polyline**: High-contrast blue corridor with ambient glow (`emergency_route_style.qml`).
