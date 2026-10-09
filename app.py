"""
Servidor receptor de localização GPS (T-PCIE + SIM7070G) para Raspberry Pi.

Endpoints:
  POST /api/location          recebe o JSON enviado pelo ESP32
  GET  /api/locations         lista as últimas posições (?limit=100&device_id=...)
  GET  /api/location/latest   última posição (?device_id=...)
  GET  /health                verificação rápida
  GET  /                      mapa web com a última posição e o trajeto
"""
import logging
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, Response, g, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GPS_DB", os.path.join(BASE_DIR, "locations.db"))

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ------------------------------------------------------------
#  Banco de dados (SQLite)
# ------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS locations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   TEXT    NOT NULL,
                timestamp   TEXT,
                lat         REAL    NOT NULL,
                lon         REAL    NOT NULL,
                alt         REAL,
                speed_kmh   REAL,
                sats_used   INTEGER,
                wifi_rssi   INTEGER,
                received_at TEXT    NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_dev_id ON locations(device_id, id DESC)")


init_db()


# ------------------------------------------------------------
#  API
# ------------------------------------------------------------
def _num(value, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


@app.post("/api/location")
def post_location():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="corpo precisa ser JSON"), 400

    lat = _num(data.get("lat"))
    lon = _num(data.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify(error="lat/lon invalidos"), 400

    device_id = str(data.get("device_id") or "desconhecido")[:64]
    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    db = get_db()
    cur = db.execute(
        """
        INSERT INTO locations
            (device_id, timestamp, lat, lon, alt, speed_kmh, sats_used, wifi_rssi, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            str(data.get("timestamp") or "")[:32] or None,
            lat,
            lon,
            _num(data.get("alt")),
            _num(data.get("speed_kmh")),
            _num(data.get("sats_used"), int),
            _num(data.get("wifi_rssi"), int),
            received_at,
        ),
    )
    db.commit()
    app.logger.info("[%s] lat=%.6f lon=%.6f alt=%s sats=%s",
                    device_id, lat, lon, data.get("alt"), data.get("sats_used"))
    return jsonify(status="ok", id=cur.lastrowid), 201


@app.get("/api/locations")
def list_locations():
    limit = min(max(request.args.get("limit", 100, type=int), 1), 5000)
    device_id = request.args.get("device_id")

    sql = "SELECT * FROM locations"
    params = []
    if device_id:
        sql += " WHERE device_id = ?"
        params.append(device_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = get_db().execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])   # mais recente primeiro


@app.get("/api/location/latest")
def latest_location():
    device_id = request.args.get("device_id")
    sql = "SELECT * FROM locations"
    params = []
    if device_id:
        sql += " WHERE device_id = ?"
        params.append(device_id)
    sql += " ORDER BY id DESC LIMIT 1"

    row = get_db().execute(sql, params).fetchone()
    if row is None:
        return jsonify(error="sem dados"), 404
    return jsonify(dict(row))


@app.get("/health")
def health():
    return jsonify(status="ok")


# ------------------------------------------------------------
#  Página do mapa (Leaflet + OpenStreetMap; o navegador precisa de internet)
# ------------------------------------------------------------
MAP_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rastreador GPS</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  html, body { height: 100%; margin: 0; font-family: system-ui, sans-serif; }
  #bar { padding: 8px 12px; background: #111; color: #eee; font-size: 14px; }
  #map { height: calc(100% - 37px); }
</style>
</head>
<body>
<div id="bar">Aguardando dados...</div>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map = L.map('map').setView([0, 0], 2);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  { maxZoom: 19, attribution: '&copy; OpenStreetMap' }).addTo(map);
const line = L.polyline([], { weight: 3 }).addTo(map);
let marker = null, centered = false;

async function refresh() {
  try {
    const r = await fetch('/api/locations?limit=200');
    const d = await r.json();
    if (!d.length) return;
    const last = d[0];
    line.setLatLngs(d.map(p => [p.lat, p.lon]).reverse());
    if (!marker) marker = L.marker([last.lat, last.lon]).addTo(map);
    else marker.setLatLng([last.lat, last.lon]);
    if (!centered) { map.setView([last.lat, last.lon], 17); centered = true; }
    document.getElementById('bar').textContent =
      last.device_id + ' | ' + (last.timestamp || last.received_at) +
      ' | ' + last.lat.toFixed(6) + ', ' + last.lon.toFixed(6) +
      ' | alt ' + (last.alt ?? '-') + ' m | sats ' + (last.sats_used ?? '-');
  } catch (e) {
    document.getElementById('bar').textContent = 'Erro ao buscar dados';
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(MAP_HTML, mimetype="text/html")


if __name__ == "__main__":
    # Modo de teste. Em produção use o gunicorn (veja o arquivo .service).
    app.run(host="0.0.0.0", port=8000)
