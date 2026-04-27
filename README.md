# 🌑 SalusShadow

**Urban shade analysis from OpenStreetMap + solar geometry.**  
Compute which sidewalks are in shade, score shade potential across a neighborhood, or query a single point in real time.

🌐 **[Live Demo →](https://cmu-salus-lab.github.io/salusshadow/)**  
📡 **[API →](https://salusshadow-api.onrender.com/)**

![SalusShadow app screenshot](docs/screenshot.png)

---

## What it does

SalusShadow casts shadows from buildings (and optionally trees) onto sidewalks using real sun angles, then measures how much of each street segment is shaded. It operates at two scales:

- **Point query** — Is this exact spot in shade right now? (used by the web app)
- **Full pipeline** — Score every sidewalk in a neighborhood, export to GeoPackage for QGIS

Shadow geometry is computed from OSM building footprints + estimated heights, solar position from [pvlib](https://pvlib-python.readthedocs.io/), and street networks from [OSMnx](https://osmnx.readthedocs.io/).

---

## Quick start (web app)

1. Open **[cmu-salus-lab.github.io/salusshadow](https://cmu-salus-lab.github.io/salusshadow/)**
2. Click anywhere on the map or drag the pin to your location of interest
3. Pick a **date and time** using the datetime picker
4. Hit **"Check shade"** — results appear in ~5–15 seconds
5. See whether the spot is ☀️ in sun or 🌑 in shade, the street shadow ratio, sun azimuth/elevation, and street-level imagery from Mapillary

> **Note:** The first request after a period of inactivity may take ~30 seconds (Render free tier cold start). Subsequent requests are fast.

---

## Repository layout

```
salusshadow/
├── salusshadow.py      # Core library — shadow geometry, pipeline, point query
├── main.py             # FastAPI wrapper (deployed to Render)
├── requirements.txt    # Python dependencies
└── index.html          # Web app (served via GitHub Pages)
```

---

## Web app

Drop a pin on the map (or type lat/lon), pick a date and time, and instantly see:

- ☀️ / 🌑 In sun or in shade
- Shadow ratio of the nearest street segment
- Sun azimuth + elevation
- Street-level imagery from Mapillary

**[→ Open the app](https://cmu-salus-lab.github.io/salusshadow/)**

> The app calls the FastAPI backend on Render. The first request after a period of inactivity may take ~30 seconds (free tier cold start).

---

## Python library

### Install dependencies

```bash
pip install osmnx pvlib geopandas shapely rasterio numpy pandas tqdm
```

### Point query

```python
import salusshadow

result = salusshadow.query_point_shade(
    lat=42.3601,
    lon=-71.0589,
    timestamp="2025-07-29T14:00:00-04:00",
    search_radius_m=200,
    include_trees=True,
)
# {
#   'in_shade': False,
#   'street_name': 'Court Street',
#   'street_osm_id': '8646962',
#   'shadow_ratio': 0.0,
#   'sun_azimuth': 216.98,
#   'sun_elevation': 62.03
# }
```

### Full neighborhood pipeline

Score every sidewalk in an area and export to GeoPackage:

```bash
python salusshadow.py \
  --place "Downtown Boston, Massachusetts, USA" \
  --dt "2025-07-29T14:00:00-04:00" \
  --include-trees \
  --save-shadows \
  --out boston_shade.gpkg
```

Or with a bounding box:

```bash
python salusshadow.py \
  --bbox "42.365,42.355,-71.055,-71.068" \
  --dt "2025-07-29T14:00:00-04:00" \
  --footway-source both \
  --alpha 3.0 \
  --out output.gpkg
```

#### Key CLI options

| Flag | Default | Description |
|---|---|---|
| `--place` or `--bbox` | — | Area to analyze (required) |
| `--dt` | — | ISO 8601 datetime with timezone (required) |
| `--include-trees` | off | Add OSM tree + tree\_row shadows |
| `--footway-source` | `osm` | `osm`, `synth` (synthesized from roads), or `both` |
| `--alpha` | `3.0` | Sun-aversion weight for experienced length scoring |
| `--save-shadows` | off | Export shadow polygons as extra layers (useful in QGIS) |
| `--default-bldg-h` | `12.0` | Fallback building height in metres |
| `--default-tree-h` | `10.0` | Default tree height in metres |
| `--sidewalk-offset` | `7.0` | Offset (m) for synthesized sidewalk lines |

---

## REST API

The FastAPI backend exposes a single endpoint:

```
GET https://salusshadow-api.onrender.com/shade
```

**Parameters**

| Param | Required | Example |
|---|---|---|
| `lat` | ✅ | `42.3601` |
| `lon` | ✅ | `-71.0589` |
| `timestamp` | ✅ | `2025-07-29T14:00:00-04:00` |
| `include_trees` | optional | `true` |
| `search_radius_m` | optional | `200` |

**Example**

```
curl "https://salusshadow-api.onrender.com/shade?lat=42.3601&lon=-71.0589&timestamp=2025-07-29T14:00:00-04:00"
```

**Response**

```json
{
  "in_shade": false,
  "street_name": "Court Street",
  "street_osm_id": "8646962",
  "shadow_ratio": 0.0,
  "sun_azimuth": 216.98,
  "sun_elevation": 62.03
}
```

Interactive docs: [https://salusshadow-api.onrender.com/docs](https://salusshadow-api.onrender.com/docs)

---

## Run locally

```bash
# Clone
git clone https://github.com/CMU-SALUS-Lab/salusshadow.git
cd salusshadow

# Install
pip install -r requirements.txt

# Start API
uvicorn main:app --reload --port 8000

# Open app — set the sidebar API URL to http://localhost:8000
open index.html
```

---

## Advanced: tree detection from aerial imagery

SalusShadow supports tree canopy segmentation via [DetecTree](https://github.com/martibosch/detectree) using NAIP (1 m) or Sentinel-2 (10 m) orthophotos from Microsoft Planetary Computer:

```bash
python salusshadow.py \
  --place "Pittsburgh, PA" \
  --dt "2025-08-01T12:00:00-04:00" \
  --auto-ortho \
  --ortho-source naip \
  --out pittsburgh_shade.gpkg
```

Requires additional packages: `detectree pystac-client stackstac planetary-computer scipy`.

---

## Output fields (GeoPackage / GeoJSON)

| Field | Description |
|---|---|
| `shade_frac` | Fraction of sidewalk segment in shadow (0–1) |
| `experienced_len` | Thermally-weighted length: `α × sun_length + shade_length` |
| `length_m` | Segment length in metres |
| `source` | `osm` or `synth` |

---

## How shadow geometry works

1. Sun azimuth and elevation are computed with `pvlib` for the given datetime and location
2. Each building polygon is extruded along the sun vector to produce a shadow polygon; the building footprint itself is subtracted so only the cast shadow remains
3. Tree shadows are approximated as buffered projections (point trees → circles, tree rows → buffered lines)
4. All shadow polygons are unioned into a single geometry
5. Each sidewalk segment is intersected with the shadow union to compute `shade_frac`

---

## Dependencies

- [OSMnx](https://osmnx.readthedocs.io/) — street networks and building footprints from OpenStreetMap
- [pvlib](https://pvlib-python.readthedocs.io/) — solar position calculations
- [GeoPandas](https://geopandas.org/) + [Shapely](https://shapely.readthedocs.io/) — spatial operations
- [FastAPI](https://fastapi.tiangolo.com/) — REST API
- [Leaflet.js](https://leafletjs.com/) — interactive map
- [Mapillary JS SDK](https://www.mapillary.com/developer) — street-level imagery

---

## License

[Apache 2.0](LICENSE) — CMU SALUS Lab
