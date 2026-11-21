"""
PyQGIS On-Demand XYZ Tile Server
Reads C:/Projects/Intelligent-Emergency-Response/gis/qgis/Intelligent_Emergency_Response.qgz
and renders crisp 256x256 PNG tiles for Leaflet at any zoom level (z=10..17)
with zero external basemap API keys. Caches rendered tiles on disk.
"""
import math
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qgis.core import (
    QgsApplication,
    QgsProject,
    QgsMapSettings,
    QgsMapRendererSequentialJob,
    QgsRectangle,
    QgsCoordinateReferenceSystem,
    QgsLineSymbol,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsFillSymbol,
    QgsSingleSymbolRenderer,
)
from qgis.PyQt.QtCore import QSize, QBuffer, QIODevice
from qgis.PyQt.QtGui import QColor

QGS_PATH = "C:/Projects/Intelligent-Emergency-Response/gis/qgis/Intelligent_Emergency_Response.qgz"
CACHE_DIR = "C:/Projects/Intelligent-Emergency-Response/gis/qgis/tile_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

app = QgsApplication([], False)
app.initQgis()

project = QgsProject.instance()
ok = project.read(QGS_PATH)
print(f"[QGIS-TILES] Loaded {QGS_PATH}: {ok}", flush=True)

# Style the 40,394-edge metropolitan road network with crisp light-cartographic hierarchy
render_layers = []
for lyr in project.mapLayers().values():
    name = lyr.name()
    if "40,394" in name:
        cats = [
            ("trunk", "Trunk Highway (NH48 / Corridor)", "#1C1D21", "1.6"),
            ("trunk_link", "Trunk Link", "#1C1D21", "1.2"),
            ("primary", "Primary Arterial", "#334155", "1.3"),
            ("primary_link", "Primary Link", "#334155", "1.0"),
            ("secondary", "Secondary Arterial", "#64748B", "1.0"),
            ("secondary_link", "Secondary Link", "#64748B", "0.8"),
            ("tertiary", "Tertiary Road", "#94A3B8", "0.75"),
            ("tertiary_link", "Tertiary Link", "#94A3B8", "0.6"),
            ("unclassified", "Unclassified Road", "#CBD5E1", "0.55"),
            ("residential", "Residential Street", "#CBD5E1", "0.45"),
            ("service", "Service Lane", "#E2E8F0", "0.35"),
            ("living_street", "Living Street", "#E2E8F0", "0.35"),
        ]
        renderer_cats = []
        for val, label, color, width in cats:
            sym = QgsLineSymbol.createSimple({"color": color, "width": width, "line_style": "solid"})
            renderer_cats.append(QgsRendererCategory(val, sym, label))
        default_sym = QgsLineSymbol.createSimple({"color": "#CBD5E1", "width": "0.4", "line_style": "solid"})
        renderer_cats.append(QgsRendererCategory("", default_sym, "Other"))
        lyr.setRenderer(QgsCategorizedSymbolRenderer("highway", renderer_cats))
        render_layers.append(lyr)
    elif "2.5km" in name:
        sym = QgsFillSymbol.createSimple({"color": "179,161,248,28", "outline_color": "143,119,237,110", "outline_width": "0.5"})
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
        render_layers.append(lyr)

# Put buffers underneath roads
render_layers.sort(key=lambda l: 0 if "40,394" in l.name() else 1)

CRS_4326 = project.crs()
RENDER_LOCK = threading.Lock()


def tile_to_4326_bbox(z: int, x: int, y: int) -> QgsRectangle:
    n = 2.0 ** z
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return QgsRectangle(lon_min, lat_min, lon_max, lat_max)


def render_tile_png(z: int, x: int, y: int) -> bytes:
    cache_path = os.path.join(CACHE_DIR, f"{z}_{x}_{y}.png")
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()

    bbox = tile_to_4326_bbox(z, x, y)
    with RENDER_LOCK:
        settings = QgsMapSettings()
        settings.setTransformContext(project.transformContext())
        settings.setLayers(render_layers)
        settings.setOutputSize(QSize(256, 256))
        settings.setDestinationCrs(CRS_4326)
        settings.setExtent(bbox)
        settings.setBackgroundColor(QColor(246, 247, 249))
        settings.setFlag(QgsMapSettings.Antialiasing, True)

        job = QgsMapRendererSequentialJob(settings)
        job.start()
        job.waitForFinished()
        img = job.renderedImage()

        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        data = bytes(buf.data())
        buf.close()

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
            self.wfile.write(b'{"status":"ok","engine":"PyQGIS","layers":' + str(len(render_layers)).encode() + b'}')
            return

        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "tiles" and parts[3].endswith(".png"):
            try:
                z = int(parts[1])
                x = int(parts[2])
                y = int(parts[3][:-4])
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
    print(f"[QGIS-TILES] Serving QGIS project tiles on http://127.0.0.1:{port}/tiles/{{z}}/{{x}}/{{y}}.png", flush=True)
    try:
        server.serve_forever()
    finally:
        app.exitQgis()
