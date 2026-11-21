"""
PyQGIS On-Demand XYZ Tile Server — Connected directly to emergency_db (public.roads)
Renders crisp, labelled 256x256 PNG tiles with road hierarchy and curved street-name
labels (QgsPalLayerSettings) — zero external tile APIs, zero purple washout.
"""
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qgis.core import (
    QgsApplication,
    QgsProject,
    QgsVectorLayer,
    QgsMapSettings,
    QgsMapRendererSequentialJob,
    QgsRectangle,
    QgsLineSymbol,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsPalLayerSettings,
    QgsTextFormat,
    QgsTextBufferSettings,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QSize, QBuffer, QIODevice
from qgis.PyQt.QtGui import QColor, QFont

CACHE_DIR = "C:/Projects/Intelligent-Emergency-Response/gis/qgis/tile_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

app = QgsApplication([], False)
app.initQgis()

project = QgsProject.instance()

# Load public.roads directly from localhost:5432 / emergency_db (unified database)
uri = (
    "dbname='emergency_db' host=localhost port=5432 user='postgres' password='Ankush@2007' "
    "sslmode=disable key='road_id' srid=4326 type=LineString table=\"public\".\"roads\" (geom) sql="
)
roads_lyr = QgsVectorLayer(uri, "Hubballi-Dharwad Roads (emergency_db)", "postgres")
print(f"[QGIS-TILES] Loaded public.roads from emergency_db: valid={roads_lyr.isValid()}", flush=True)

# Clean cartographic road hierarchy (NO purple buffer washout!)
cats = [
    ("trunk", "National / Trunk Highway", "#D97706", "2.6"),
    ("trunk_link", "Trunk Link", "#D97706", "1.8"),
    ("primary", "Primary Arterial", "#1E293B", "2.0"),
    ("primary_link", "Primary Link", "#1E293B", "1.4"),
    ("secondary", "Secondary Road", "#475569", "1.4"),
    ("secondary_link", "Secondary Link", "#475569", "1.0"),
    ("tertiary", "Tertiary Road", "#64748B", "1.0"),
    ("tertiary_link", "Tertiary Link", "#64748B", "0.8"),
    ("unclassified", "Local Road", "#94A3B8", "0.75"),
    ("residential", "Residential Street", "#CBD5E1", "0.65"),
    ("service", "Service Lane", "#E2E8F0", "0.45"),
    ("living_street", "Living Street", "#E2E8F0", "0.45"),
]
renderer_cats = []
for val, label, color, width in cats:
    sym = QgsLineSymbol.createSimple({"color": color, "width": width, "line_style": "solid", "capstyle": "round", "joinstyle": "round"})
    renderer_cats.append(QgsRendererCategory(val, sym, label))
default_sym = QgsLineSymbol.createSimple({"color": "#CBD5E1", "width": "0.55", "line_style": "solid"})
renderer_cats.append(QgsRendererCategory("", default_sym, "Other"))
roads_lyr.setRenderer(QgsCategorizedSymbolRenderer("highway", renderer_cats))

# Enable crisp curved road-name labels with white halo buffer
pal = QgsPalLayerSettings()
pal.fieldName = "name"
pal.enabled = True
pal.placement = QgsPalLayerSettings.Curved
pal.repeatDistance = 120
pal.minFeatureSize = 12

fmt = QgsTextFormat()
font = QFont("Inter", 8)
font.setBold(True)
fmt.setFont(font)
fmt.setSize(8)
fmt.setColor(QColor("#1E293B"))

buf = QgsTextBufferSettings()
buf.setEnabled(True)
buf.setSize(1.2)
buf.setColor(QColor("#FFFFFF"))
fmt.setBuffer(buf)
pal.setFormat(fmt)

roads_lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
roads_lyr.setLabelsEnabled(True)

project.addMapLayer(roads_lyr)
render_layers = [roads_lyr]

CRS_4326 = roads_lyr.crs()
RENDER_LOCK = threading.Lock()


def tile_to_4326_bbox(z: int, x: int, y: int) -> QgsRectangle:
    n = 2.0 ** z
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return QgsRectangle(lon_min, lat_min, lon_max, lat_max)


def render_tile_png(z: int, x: int, y: int) -> bytes:
    cache_path = os.path.join(CACHE_DIR, f"v3_{z}_{x}_{y}.png")
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()

    bbox = tile_to_4326_bbox(z, x, y)
    # 3-Tier Multi-Scale Filtering (display only; routing graph untouched)
    if z <= 10:
        subset = "highway IN ('trunk','trunk_link','primary','primary_link')"
    elif z <= 13:
        subset = "highway IN ('trunk','trunk_link','primary','primary_link','secondary','secondary_link','tertiary')"
    else:
        subset = ""

    with RENDER_LOCK:
        roads_lyr.setSubsetString(subset)
        settings = QgsMapSettings()
        settings.setTransformContext(project.transformContext())
        settings.setLayers(render_layers)
        settings.setOutputSize(QSize(256, 256))
        settings.setDestinationCrs(CRS_4326)
        settings.setExtent(bbox)
        settings.setBackgroundColor(QColor(248, 249, 250))
        settings.setFlag(QgsMapSettings.Antialiasing, True)
        settings.setFlag(QgsMapSettings.DrawLabeling, z >= 13)

        job = QgsMapRendererSequentialJob(settings)
        job.start()
        job.waitForFinished()
        img = job.renderedImage()

        qbuf = QBuffer()
        qbuf.open(QIODevice.WriteOnly)
        img.save(qbuf, "PNG")
        data = bytes(qbuf.data())
        qbuf.close()

    try:
        with open(cache_path, "wb") as f:
            f.write(data)
    except Exception:
        pass
    return data


class TileHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","engine":"PyQGIS","source":"emergency_db.public.roads","labeled":true}')
            return

        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "tiles" and parts[3].endswith(".png"):
            try:
                z, x, y = int(parts[1]), int(parts[2]), int(parts[3][:-4])
                if z < 8 or z > 18:
                    self.send_error(400, "Zoom out of range")
                    return
                png = render_tile_png(z, x, y)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(png)
                return
            except Exception as e:
                self.send_error(500, str(e))
                return
        self.send_error(404, "Not Found")


if __name__ == "__main__":
    port = int(os.environ.get("QGIS_TILE_PORT", "8001"))
    server = ThreadingHTTPServer(("127.0.0.1", port), TileHandler)
    print(f"[QGIS-TILES] Serving labelled emergency_db tiles on http://127.0.0.1:{port}/tiles/{{z}}/{{x}}/{{y}}.png", flush=True)
    try:
        server.serve_forever()
    finally:
        app.exitQgis()
