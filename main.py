"""
SalusShadow API — FastAPI wrapper for query_point_shade()
Deploy to Render / Railway / Hugging Face Spaces.

Install deps:
    pip install fastapi uvicorn httpx salusshadow osmnx pvlib geopandas shapely rasterio

Run locally:
    uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
import traceback
import httpx

import salusshadow  # your salusshadow.py must be in the same directory

# Mapillary token — proxied server-side to avoid browser CORS blocks
MAPILLARY_TOKEN = "MLY|24137344532569728|c3975960ec08fefe128244f36290a00f"

app = FastAPI(
    title="SalusShadow API",
    description="Check if a lat/lon point is in shade at a given time.",
    version="1.0.0",
)

# Allow requests from GitHub Pages (and localhost for dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten to your GH Pages URL in production
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "ok", "message": "SalusShadow API is running."}


@app.get("/shade")
def get_shade(
    lat: float = Query(..., description="Latitude, e.g. 42.3601"),
    lon: float = Query(..., description="Longitude, e.g. -71.0589"),
    timestamp: str = Query(
        None,
        description="ISO 8601 datetime with timezone, e.g. 2025-07-29T14:00:00-04:00. "
                    "Defaults to current UTC time.",
    ),
    search_radius_m: float = Query(200, ge=50, le=1000, description="Search radius in metres"),
    include_trees: bool = Query(True, description="Include tree shadows"),
):
    """
    Returns shade information for the given point and time.

    Response fields:
    - in_shade (bool)
    - street_name (str | null)
    - street_osm_id (str | null)
    - shadow_ratio (float 0–1)
    - sun_azimuth (float degrees)
    - sun_elevation (float degrees)
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()

    try:
        result = salusshadow.query_point_shade(
            lat=lat,
            lon=lon,
            timestamp=timestamp,
            search_radius_m=search_radius_m,
            include_trees=include_trees,
        )
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mapillary")
async def get_mapillary(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    """
    Proxy Mapillary image lookup server-side to avoid browser CORS restrictions.
    Tries increasing search radii (100m → 250m → 500m) until images are found.
    Returns { data: [ { id, thumb_256_url, captured_at }, ... ] }
    """
    fields = "id,thumb_256_url,captured_at"

    async with httpx.AsyncClient(timeout=10.0) as client:
        for radius in [100, 250, 500]:
            url = (
                f"https://graph.mapillary.com/images"
                f"?access_token={MAPILLARY_TOKEN}"
                f"&fields={fields}"
                f"&closeto={lon},{lat}"
                f"&radius={radius}"
                f"&limit=2"
            )
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    if data:
                        return {"data": data, "radius_used": radius}
            except Exception as e:
                print(f"[DEBUG] Mapillary fetch failed at radius {radius}: {e}")
                continue

    return {"data": [], "radius_used": None}
