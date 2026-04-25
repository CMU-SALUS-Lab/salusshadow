# -*- coding: utf-8 -*-
"""
shade_potential.py — Shade-based sidewalk potential
Robust to older OSMnx versions (no reliance on osmnx.projection.get_utm_crs or ox.project_gdf).
Also handles Shapely 2.0 union_all() vs unary_union.

# Save sidewalks + shadow polygons into one GeoPackage with multiple layers
python shade_potential.py \
  --place "Downtown Boston, Massachusetts, USA" \
  --dt "2025-07-29T14:00:00-04:00" \
  --include-trees \
  --save-shadows \
  --out boston_shade.gpkg

python shade_potential.py --place "Downtown Boston, Massachusetts, USA" --dt "2025-07-29T14:00:00-04:00" --include-trees --save-shadows --out boston_shade.gpkg

"""

import argparse
import sys
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon, LineString, Point, MultiLineString
from shapely.ops import unary_union
from shapely import affinity
import osmnx as ox
import pvlib
from sympy import python
import rasterio as rio
from affine import Affine
from shapely.geometry.base import BaseGeometry

try:
    from tqdm import tqdm
    TQDM = True
except Exception:
    TQDM = False

# ------------------------------ Config defaults ------------------------------
FLOOR_HEIGHT_M = 3.2
DEFAULT_BUILDING_HEIGHT_M = 12.0
DEFAULT_TREE_HEIGHT_M = 10.0
DEFAULT_CROWN_RADIUS_M = 3.5

SIDEWALK_OFFSET_M = 7.0
SUN_ELEV_MIN_DEG = 1.0

# ------------------------------ Helpers ------------------------------
def parse_bbox(bbox_str: str) -> Tuple[float, float, float, float]:
    parts = [p.strip() for p in bbox_str.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be 'north,south,east,west'")
    north, south, east, west = map(float, parts)
    if south > north:
        raise ValueError("south must be <= north")
    if west > east:
        raise ValueError("west must be <= east")
    return north, south, east, west

def to_utc_timestamp(dt_str: str) -> pd.Timestamp:
    ts = pd.to_datetime(dt_str)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts

def get_sun(dt_utc: pd.Timestamp, lat: float, lon: float) -> Tuple[float, float]:
    sp = pvlib.solarposition.get_solarposition(dt_utc, lat, lon)
    elev = float(sp['apparent_elevation'].iloc[0])
    az   = float(sp['azimuth'].iloc[0])
    return az, elev

def estimate_building_height(attrs: dict) -> float:
    for key in ('building:height', 'height'):
        if key in attrs and attrs[key] not in (None, np.nan):
            h = str(attrs[key]).lower().strip()
            if h.endswith('m'):
                h = h[:-1]
            try:
                return float(h)
            except Exception:
                pass
    for key in ('building:levels', 'levels'):
        if key in attrs and attrs[key] not in (None, np.nan):
            try:
                return float(attrs[key]) * FLOOR_HEIGHT_M
            except Exception:
                pass
    return DEFAULT_BUILDING_HEIGHT_M

def building_shadow(poly: Polygon, height_m: float, az_deg: float, elev_deg: float):
    """Compute the shadow cast by a building polygon, EXCLUDING the building footprint itself."""
    if poly is None or poly.is_empty or elev_deg <= SUN_ELEV_MIN_DEG:
        return None
    L = height_m / np.tan(np.deg2rad(elev_deg))
    dx = -L * np.sin(np.deg2rad(az_deg))
    dy = -L * np.cos(np.deg2rad(az_deg))
    P_t = affinity.translate(poly, xoff=dx, yoff=dy)

    try:
        coords = list(poly.exterior.coords)
    except Exception:
        return None

    quads = []
    for i in range(len(coords) - 1):
        x1, y1 = coords[i]
        x2, y2 = coords[i + 1]
        xt1, yt1 = x1 + dx, y1 + dy
        xt2, yt2 = x2 + dx, y2 + dy
        quads.append(Polygon([(x1, y1), (x2, y2), (xt2, yt2), (xt1, yt1)]))

    try:
        shadow_raw = unary_union([P_t] + quads)
    except Exception:
        shadow_raw = P_t

    # Exclude the building footprint itself from the shadow polygon
    try:
        shadow_only = shadow_raw.difference(poly)
        return shadow_only if not shadow_only.is_empty else None
    except Exception:
        return shadow_raw

def tree_shadow_geom(geom, h_m: float, crown_r_m: float, az_deg: float, elev_deg: float):
    if elev_deg <= SUN_ELEV_MIN_DEG or geom is None or geom.is_empty:
        return None
    L = h_m / np.tan(np.deg2rad(elev_deg))
    dx = -L * np.sin(np.deg2rad(az_deg))
    dy = -L * np.cos(np.deg2rad(az_deg))
    try:
        if isinstance(geom, Point):
            p2 = affinity.translate(geom, xoff=dx, yoff=dy)
            cast = LineString([geom, p2]).buffer(crown_r_m)
            under = geom.buffer(crown_r_m)
            return unary_union([cast, under])
        elif isinstance(geom, LineString):
            line2 = affinity.translate(geom, xoff=dx, yoff=dy)
            return unary_union([geom.buffer(crown_r_m), line2.buffer(crown_r_m)])
        else:
            return None
    except Exception:
        return None

def synthesize_sidewalks(roads_gdf: gpd.GeoDataFrame, offset_m: float = SIDEWALK_OFFSET_M) -> gpd.GeoDataFrame:
    geoms = []
    for geom in roads_gdf.geometry:
        if geom is None or geom.is_empty:
            continue
        parts = [geom] if isinstance(geom, LineString) else list(geom.geoms) if hasattr(geom, "geoms") else []
        for ln in parts:
            for side in ('left', 'right'):
                try:
                    off = ln.parallel_offset(offset_m, side, join_style=2, mitre_limit=2.0)
                    if off is None or off.is_empty:
                        continue
                    if isinstance(off, Polygon):
                        off = LineString(off.exterior.coords)
                    geoms.append(off)
                except Exception:
                    continue
    return gpd.GeoDataFrame({"geometry": geoms, "source": "synth"}, crs=roads_gdf.crs)

def explode_lines(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    out = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if isinstance(geom, LineString):
            out.append(row)
        elif isinstance(geom, MultiLineString):
            for part in geom.geoms:
                nr = row.copy()
                nr.geometry = part
                out.append(nr)
        else:
            try:
                b = geom.boundary
                if isinstance(b, (LineString, MultiLineString)):
                    nr = row.copy()
                    nr.geometry = b if isinstance(b, LineString) else list(b.geoms)[0]
                    out.append(nr)
            except Exception:
                continue
    if not out:
        return gdf.copy()
    return gpd.GeoDataFrame(out, crs=gdf.crs).reset_index(drop=True)

def shaded_fraction(line: LineString, shadow_union) -> float:
    if shadow_union is None or line.length == 0:
        return 0.0
    inter = line.intersection(shadow_union)
    try:
        L = inter.length
    except Exception:
        L = 0.0
        if hasattr(inter, "geoms"):
            L = sum([g.length for g in inter.geoms if hasattr(g, "length")])
    return float(np.clip(L / line.length, 0.0, 1.0))

def fetch_osm_layers(place: Optional[str], bbox: Optional[Tuple[float,float,float,float]], network_type: str):
    if place:
        G = ox.graph_from_place(place, network_type=network_type)
    else:
        north, south, east, west = bbox
        G = ox.graph_from_bbox(north, south, east, west, network_type=network_type)
    return G

def fetch_osm_geometries(place: Optional[str], bbox: Optional[Tuple[float,float,float,float]], tags: dict) -> gpd.GeoDataFrame:
    """
    Version-robust getter for OSM features. Tries several OSMnx APIs depending on what's available.
    Order (place): geometries_from_place -> features_from_place -> geocode_to_gdf + geometries_from_polygon -> ... -> *_from_bbox
    """
    def _try_funcs(func_names, *args, **kwargs):
        for fname in func_names:
            fn = getattr(ox, fname, None)
            if callable(fn):
                try:
                    return fn(*args, **kwargs)
                except Exception:
                    pass
            for sub in ("geometries", "features"):
                mod = getattr(ox, sub, None)
                if mod is not None:
                    fn = getattr(mod, fname, None)
                    if callable(fn):
                        try:
                            return fn(*args, **kwargs)
                        except Exception:
                            pass
        return None

    if place:
        g = _try_funcs(["geometries_from_place", "features_from_place"], place, tags=tags)
        if g is not None:
            return g
        area = ox.geocode_to_gdf(place)
        poly = area.geometry.iloc[0]
        g = _try_funcs(["geometries_from_polygon", "features_from_polygon"], poly, tags=tags)
        if g is not None:
            return g
        north, south, east, west = poly.bounds[3], poly.bounds[1], poly.bounds[2], poly.bounds[0]
        g = _try_funcs(["geometries_from_bbox", "features_from_bbox"], north, south, east, west, tags=tags)
        if g is not None:
            return g
        raise AttributeError("No compatible OSMnx geometries/features API found for 'place'.")
    else:
        north, south, east, west = bbox
        g = _try_funcs(["geometries_from_bbox", "features_from_bbox"], north, south, east, west, tags=tags)
        if g is not None:
            return g
        raise AttributeError("No compatible OSMnx geometries/features API found for 'bbox'.")

def compute_local_utm_crs_from_wgs(gdf_wgs: gpd.GeoDataFrame) -> str:
    if gdf_wgs.crs is None:
        gdf_wgs = gdf_wgs.set_crs("EPSG:4326", allow_override=True)
    g = gdf_wgs.to_crs("EPSG:4326")
    try:
        u = g.union_all()
    except Exception:
        u = g.unary_union
    c = u.centroid
    lon, lat = c.x, c.y
    zone = int((lon + 180.0) // 6.0) + 1
    if lat >= 0:
        epsg = 32600 + zone
    else:
        epsg = 32700 + zone
    return f"EPSG:{epsg}", lat, lon

def polygons_from_detectree(ortho_path: str, utm_crs: str,
                            min_area_m2: float = 3.0, smooth_iter: int = 1) -> gpd.GeoDataFrame:
    import detectree as dtr
    import rasterio as rio
    from rasterio.features import shapes
    from shapely.geometry import shape
    import numpy as np
    try:
        from scipy.ndimage import binary_opening, binary_closing
    except Exception:
        binary_opening = binary_closing = None

    # 1) Predict tree mask (DetecTree will download its pre-trained model if needed)
    clf = dtr.Classifier()  # or dtr.Classifier(model_uri='...') to pin a model
    y_pred = clf.predict_img(ortho_path)  # returns 2D array of 0/1 labels

    mask = (y_pred > 0)  # ensure boolean
    if smooth_iter and binary_opening is not None:
        mask = binary_opening(mask, iterations=smooth_iter)
        mask = binary_closing(mask, iterations=smooth_iter)

    # 2) Polygonize using image geotransform
    with rio.open(ortho_path) as src:
        transform = src.transform
        crs = src.crs
        if crs is None:
            raise ValueError("Orthophoto must be georeferenced (CRS missing).")
        # iterate shapes only for True pixels
        geoms = []
        for geom, val in shapes(mask.astype("uint8"), mask=mask, transform=transform):
            if val != 1:
                continue
            poly = shape(geom)
            geoms.append(poly)

    # after you’ve built the list `geoms`
    n = len(geoms)
    gdf = gpd.GeoDataFrame(
            {"source": ["detectree"] * n},   # length matches geometry
            geometry=geoms,
        crs=crs
)
    gdf = gdf.to_crs(utm_crs)

    # 3) Filter tiny specks (area in m², because utm_crs is metric)
    if min_area_m2 > 0:
        gdf = gdf[gdf.area >= float(min_area_m2)].copy()

    # optional light clean-up
    try:
        gdf["geometry"] = gdf.buffer(0)  # fix slivers
    except Exception:
        pass
    return gdf.reset_index(drop=True)

def download_naip_clip_old(bbox, out_tif):
    """
    bbox = (north, south, east, west) in WGS84 degrees
    Saves COG GeoTIFF clipped to bbox at out_tif.
    Requires: pystac_client, stackstac, rasterio.
    """
    import pystac_client, stackstac, rasterio
    from rasterio.transform import from_bounds
    from rasterio.merge import merge  # optional
    import xarray as xr

    Debug = True # for testing
    north, south, east, west = bbox
    # make sure bbox spans ≥120 m in each direction (0.001° lat ≈ 111 m at 42°N)
    min_span = 0.001
    if (north - south) < min_span:
        pad = (min_span - (north - south)) / 2
        north, south = north + pad, south - pad
    if (east - west) < min_span:
        pad = (min_span - (east - west)) / 2
        east, west = east + pad, west - pad
    # -------------------------------------------------------------------------------
    
    print(f"Downloading NAIP for bbox: {north}, {south}, {east}, {west}") if Debug else None
    catalogue = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1")
    items = catalogue.search(
        collections=["naip"],
        bbox=[west, south, east, north],
        query={"gsd": {"lt": 1.5}}      # grab highest res
    ).item_collection()
    # turn into list and sort newest-first by acquisition time
    items = sorted(list(items), key=lambda itm: itm.datetime, reverse=True)
    print(f"NAIP items found: {len(items)}") if Debug else None
    if len(items) == 0:
        raise RuntimeError("No NAIP scenes intersect bbox")
    # Build a mosaic xarray DataArray
    # mosaic = stackstac.stack(items, assets=["image"], resolution=1.0,
    #                          bounds_latlon=[west, south, east, north]).isel(band=0)
    mosaic = stackstac.stack(
        items,
        assets=["R", "G", "B"], # NIR band (N) is ignored
        resolution=1.0,          # 1-m NAIP
        bounds_latlon=[west, south, east, north],
        epsg=4326                # prevent "ERROR: Cannot pick a common CRS, since asset 'image' of item 0 'ma_m_4207148_nw_19_1_20120710_20120926' does not have one.". 
    ).isel(time=0)               # pick newest acquisition (We request only one date)
    # "sortby("time")" orders all scenes chronologically.
    print(f"NAIP mosaic shape: {mosaic.shape}, coords: {mosaic.coords}") if Debug else None
    
    # ----- guard against empty array ---------------------------------
    if mosaic.sizes["x"] == 0 or mosaic.sizes["y"] == 0:
        print("NAIP returned empty array, trying to pad bbox slightly...") if Debug else None
        # Grow bbox by if 1 m (~0.00001°) on each side
        pad = 0.02
        mosaic = stackstac.stack(
            items, assets=["R", "G", "B"],
            resolution=1.0,
            bounds_latlon=[west - pad, south - pad, east + pad, north + pad],
            epsg=4326
        ).isel(time=0)
    if mosaic.sizes["x"] == 0 or mosaic.sizes["y"] == 0:
        raise RuntimeError("NAIP still empty – consider --ortho-source sentinel")
    # ------------------------------------------------------------------
    
    # (bands, y, x) -> (y, x, bands)
    print(f"NAIP mosaic shape: {mosaic.shape}, coords: {mosaic.coords}") if Debug else None
    arr = mosaic.transpose("y", "x", "band").values
    print(f"NAIP raw shape: {arr.shape}, dtype={arr.dtype}") if Debug else None
    transform = from_bounds(west, south, east, north,
                            width=arr.shape[1], height=arr.shape[0])
    print(f"NAIP clip shape: {arr.shape}, dtype={arr.dtype}, transform={transform}") if Debug else None

    # Save as COG GeoTIFF
    with rasterio.open(
        out_tif, "w",
        driver="COG", height=arr.shape[0], width=arr.shape[1],
        count=3, dtype=arr.dtype, crs="EPSG:4326", transform=transform,
        compress="lzw"
    ) as dst:
        for b in range(3):
            dst.write(arr[:, :, b], b + 1)
            
def download_naip_clip(bbox_deg, out_tif):
    """
    bbox_deg : (N, S, E, W) in WGS-84 degrees
    Saves an RGB COG clipped to *exactly* that bbox.
    """
    import pystac_client, rasterio, stackstac
    from rasterio.mask import mask
    from rasterio.transform import from_bounds
    from shapely.geometry import box, mapping
    import numpy as np

    N, S, E, W = bbox_deg
    geom_bbox = box(W, S, E, N)

    # ── fetch items ───────────────────────────────────────────────────────────
    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1")
    items = cat.search(
        collections=["naip"],
        bbox=[W, S, E, N],
        query={"gsd": {"lt": 1.5}}
    ).item_collection()

    if len(items) == 0:
        raise RuntimeError("No NAIP scenes for this bbox")

    # newest scene first
    items = sorted(list(items), key=lambda itm: itm.datetime, reverse=True)

    # ── build a lazy stack of full tiles (no bounds_latlon) ───────────────────
    stk = stackstac.stack(
        items[:1],                 # just the most-recent tile set
        assets=["R", "G", "B"],
        epsg=4326                  # NAIP native CRS
    ).isel(time=0)                 # drop time dim

    # stk dims: band,y,x ; write to temp VRT so we can use rasterio.mask
    vrt = stk.rio.write_crs("EPSG:4326").rio.to_raster("/tmp/_naip_tmp.vrt")

    with rasterio.open(vrt) as src:
        out, transform = mask(src, [mapping(geom_bbox)], crop=True)
        # out shape -> (bands, rows, cols)

    if out.size == 0:
        raise RuntimeError("Still no pixels – NAIP gap here, use --ortho-source sentinel")

    # ── save COG ──────────────────────────────────────────────────────────────
    height, width = out.shape[1], out.shape[2]
    profile = {
        "driver": "COG",
        "height": height,
        "width":  width,
        "count":  3,
        "dtype":  out.dtype,
        "crs":    "EPSG:4326",
        "transform": transform,
        "compress": "lzw"
    }
    with rasterio.open(out_tif, "w", **profile) as dst:
        dst.write(out)

    print(f"NAIP orthophoto saved → {out_tif}")
    return out_tif

# --------------------------------------------------------------------
# Sentinel-2 download helper (Planetary Computer, 10 m “visual” RGB)
# --------------------------------------------------------------------
def download_sentinel_clip(bbox_deg, out_tif):
    import pystac_client, stackstac, rasterio, planetary_computer as pc
    from rasterio.transform import from_bounds

    N, S, E, W = bbox_deg
    cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1")
    items = cat.search(
        collections=["sentinel-2-l2a"],
        bbox=[W, S, E, N],
        query={"eo:cloud_cover": {"lt": 20}}
    ).item_collection()

    if len(items) == 0:
        raise RuntimeError("No Sentinel-2 scenes here")

    # sign every item so asset URLs include SAS token
    items = [pc.sign(i) for i in items]

    # pick clearest / newest
    items = sorted(items, key=lambda i: (i.properties["eo:cloud_cover"], i.datetime))

    code = int(str(items[0].properties["proj:code"]).split(":")[-1])  # e.g. 32619
    da = stackstac.stack(
        items[:1],
        assets=["B04", "B03", "B02"],   # R,G,B single bands
        resolution=10,
        bounds_latlon=[W, S, E, N],
        epsg=code
    ).isel(time=0)

    arr = da.transpose("y", "x", "band").values
    # --- get transform without rioxarray ---------------------------------
    if "transform" in da.attrs:
        transform = Affine(*da.attrs["transform"][:6])   # convert tuple to Affine
    else:
        # fallback: build from x/y coords
        res_x = (da.x[1] - da.x[0]).item()
        res_y = (da.y[0] - da.y[1]).item()  # note sign
        transform = Affine.translation(da.x.min().item(), da.y.max().item()) * Affine.scale(res_x, res_y)
    # ---------------------------------------------------------------------
    crs_out   = f"EPSG:{code}"

    with rasterio.open(
        out_tif, "w", driver="COG",
        height=arr.shape[0], width=arr.shape[1],
        count=3, dtype=arr.dtype, crs=crs_out,
        transform=transform, compress="lzw"
    ) as dst:
        for b in range(3):
            dst.write(arr[:, :, b], b + 1)

    print(f"Sentinel-2 orthophoto saved → {out_tif}")
    return out_tif


# ------------------------------ Main pipeline ------------------------------------------------------------------
def run_pipeline(place: Optional[str],
                 bbox: Optional[Tuple[float,float,float,float]],
                 dt_utc: pd.Timestamp,
                 alpha: float,
                 include_trees: bool,
                 footway_source: str,
                 out_path: str,
                 floor_height: float = FLOOR_HEIGHT_M,
                 default_bldg_h: float = DEFAULT_BUILDING_HEIGHT_M,
                 default_tree_h: float = DEFAULT_TREE_HEIGHT_M,
                 crown_radius: float = DEFAULT_CROWN_RADIUS_M,
                 sidewalk_offset_m: float = SIDEWALK_OFFSET_M,
                 save_shadows: bool = False,
                 detectree_ortho: Optional[str] = None,
                 detectree_min_area: float = 3.0,
                 detectree_smooth: int = 1,
                 auto_ortho: bool = False,
                 ortho_source: str = "naip") -> None:
    """
    Main pipeline to compute shade-based sidewalk potential.
    """
    if not out_path.lower().endswith((".gpkg", ".geojson")):
        raise ValueError("Output path must end with .gpkg or .geojson")

    # Set global defaults for building/tree heights, crown radius, etc.

    global FLOOR_HEIGHT_M, DEFAULT_BUILDING_HEIGHT_M, DEFAULT_TREE_HEIGHT_M, DEFAULT_CROWN_RADIUS_M
    FLOOR_HEIGHT_M = floor_height
    DEFAULT_BUILDING_HEIGHT_M = default_bldg_h
    DEFAULT_TREE_HEIGHT_M = default_tree_h
    DEFAULT_CROWN_RADIUS_M = crown_radius

    # 1) Fetch networks
    print("[1/6] Downloading OSM networks...")
    G_drive = fetch_osm_layers(place, bbox, "drive")
    G_walk  = fetch_osm_layers(place, bbox, "walk")

    roads_wgs = ox.graph_to_gdfs(G_drive, nodes=False, edges=True)
    walk_edges_wgs = ox.graph_to_gdfs(G_walk, nodes=False, edges=True)[["geometry"]]
    if roads_wgs.crs is None:
        roads_wgs.set_crs("EPSG:4326", inplace=True, allow_override=True)
    if walk_edges_wgs.crs is None:
        walk_edges_wgs.set_crs("EPSG:4326", inplace=True, allow_override=True)

    # 2) Pick a local UTM and project
    print("[2/6] Projecting to local metric CRS...")
    utm_crs, lat_c, lon_c = compute_local_utm_crs_from_wgs(roads_wgs if len(roads_wgs) else walk_edges_wgs)
    roads = roads_wgs.to_crs(utm_crs)
    walk_edges = walk_edges_wgs.to_crs(utm_crs)

    # 3) Sidewalks: OSM-mapped, synthesized, or both
    print(f"[3/6] Building sidewalk layer (source={footway_source})...")
    gdfs = []
    if footway_source in ("osm", "both"):
        sw_osm = walk_edges.copy()
        sw_osm["source"] = "osm"
        gdfs.append(sw_osm)
    if footway_source in ("synth", "both"):
        sw_syn = synthesize_sidewalks(roads, offset_m=sidewalk_offset_m)
        gdfs.append(sw_syn)
    sidewalks_all = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=utm_crs)
    sidewalks_all = explode_lines(sidewalks_all)

    # 4) Buildings + optional trees
    print("[4/6] Downloading buildings (and trees if requested)...")
    bldgs_raw = fetch_osm_geometries(place, bbox, tags={"building": True})
    if bldgs_raw.crs is None:
        bldgs_raw.set_crs("EPSG:4326", inplace=True, allow_override=True)
    bldgs_raw = bldgs_raw.to_crs(utm_crs)
    bldgs = bldgs_raw[bldgs_raw.geometry.type.isin(["Polygon","MultiPolygon"])].copy()
    bldgs["H"] = [
        estimate_building_height(attrs) for attrs in bldgs.drop(columns="geometry").to_dict(orient="records")
    ]

    trees_pt = trees_row = None
    if include_trees:
        try:
            trees_pt = fetch_osm_geometries(place, bbox, tags={"natural": "tree"})
            if trees_pt.crs is None:
                trees_pt.set_crs("EPSG:4326", inplace=True, allow_override=True)
            trees_pt = trees_pt.to_crs(utm_crs)
        except Exception:
            trees_pt = None
        try:
            trees_row = fetch_osm_geometries(place, bbox, tags={"natural": "tree_row"})
            if trees_row.crs is None:
                trees_row.set_crs("EPSG:4326", inplace=True, allow_override=True)
            trees_row = trees_row.to_crs(utm_crs)
        except Exception:
            trees_row = None
    # Optional: add DetecTree canopy polygons as additional tree sources
    detectree_gdf = None
    if auto_ortho:
        if not bbox and not place:
            raise ValueError("--auto-ortho needs --bbox or --place")
        # derive bbox (in WGS84) from place if needed
        if place:
            gdf_place = ox.geocode_to_gdf(place)
            bbox_deg = gdf_place.total_bounds[3], gdf_place.total_bounds[1], \
                    gdf_place.total_bounds[2], gdf_place.total_bounds[0]
        else:
            bbox_deg = bbox
        # ortho_path = Path(out_path).with_suffix(".ortho.tif") 
        ortho_path = out_path[:-8] + "_ortho.tif" if out_path.lower().endswith(".geojson") else out_path[:-5] + "_ortho.tif"
        if ortho_source not in ("naip", "sentinel"):
            raise ValueError("Invalid --ortho-source, must be 'naip' or 'sentinel'")
        print(f"[4a/6] Downloading orthophoto from {ortho_source} for DetecTree...")
        # download NAIP or Sentinel-2 imagery
        if ortho_source == "naip":
            download_naip_clip(bbox_deg, ortho_path)
        else:
            download_sentinel_clip(bbox_deg, ortho_path)   # similar helper
        detectree_ortho = str(ortho_path)   # re-use the DetecTree pathway

    if detectree_ortho:
        print("[4b/6] Segmenting trees with DetecTree from orthophoto…")
        detectree_gdf = polygons_from_detectree(
            detectree_ortho, utm_crs,
            min_area_m2=detectree_min_area,
            smooth_iter=detectree_smooth
        )

    # 5) Sun and shadow polygons
    print("[5/6] Computing sun position and shadows...")
    az, elev = get_sun(dt_utc, lat_c, lon_c)
    print(f"    Sun azimuth={az:.1f}°, elevation={elev:.1f}°")

    b_shadows = []
    it = bldgs.itertuples()
    if TQDM:
        it = tqdm(it, total=len(bldgs), desc="    Buildings")
    for row in it:
        geom = row.geometry
        # bid  = getattr(row, "osmid", None)   # element_id
        bid  = getattr(row, "osmid", getattr(row, "element_id", None))
        try:
            geom = geom.buffer(0)
        except Exception:
            pass
        h = getattr(row, "H", DEFAULT_BUILDING_HEIGHT_M)
        sp = building_shadow(geom, h, az, elev)
        if sp is not None and not sp.is_empty:
            # b_shadows.append(sp)
            b_shadows.append((sp, bid))

    t_shadows = []
    if include_trees:
        if trees_pt is not None and len(trees_pt) > 0:
            itp = trees_pt.geometry
            if TQDM:
                itp = tqdm(itp, total=len(trees_pt), desc="    Trees (points)")
            for p in itp:
                sp = tree_shadow_geom(p, DEFAULT_TREE_HEIGHT_M, DEFAULT_CROWN_RADIUS_M, az, elev)
                if sp is not None and not sp.is_empty:
                    t_shadows.append(sp)
        if trees_row is not None and len(trees_row) > 0:
            itr = trees_row.geometry
            if TQDM:
                itr = tqdm(itr, total=len(trees_row), desc="    Tree rows")
            for ln in itr:
                sp = tree_shadow_geom(ln, DEFAULT_TREE_HEIGHT_M, DEFAULT_CROWN_RADIUS_M, az, elev)
                if sp is not None and not sp.is_empty:
                    t_shadows.append(sp)
        # Treat DetecTree canopy polygons as “flat crowns” at tree height
        if detectree_gdf is not None and len(detectree_gdf) > 0:
            itp = detectree_gdf.geometry
            if TQDM:
                itp = tqdm(itp, total=len(detectree_gdf), desc="    Tree canopy (DetecTree)")
            for crown in itp:
                sp = building_shadow(crown, DEFAULT_TREE_HEIGHT_M, az, elev)
                if sp is not None and not sp.is_empty:
                    t_shadows.append(sp)
                    
    shadow_union = None
    if b_shadows or t_shadows:
        # keep only the geometry object whether entry is geom or (geom, id)
        all_geoms = [(g if isinstance(g, BaseGeometry) else g[0])
                    for g in (b_shadows + t_shadows)]
        print(f"    Unioning {len(all_geoms)} shadow parts...")
        shadow_union = unary_union(all_geoms)

   
    # --- Optional export of shadow polygons (sp) for QGIS ---
    if save_shadows and shadow_union is not None:
        # parts_geoms, parts_src = [], []
        parts_geoms, parts_src, parts_id = [], [], []
        for sp, bid in b_shadows:
            parts_geoms.append(sp)
            parts_src.append("building")
            parts_id.append(bid)
        for sp in t_shadows:
            parts_geoms.append(sp)
            parts_src.append("tree")
            parts_id.append(None)  # or "detectree"/"osm"
        # gdf_parts = gpd.GeoDataFrame({"source": parts_src}, geometry=parts_geoms, crs=utm_crs)
        gdf_parts = gpd.GeoDataFrame({"source": parts_src, "osmid": parts_id}, geometry=parts_geoms, crs=utm_crs)

        union_series = gpd.GeoSeries([shadow_union], crs=utm_crs)
        try:
            gdf_union = gpd.GeoDataFrame(geometry=union_series.explode(index_parts=False), crs=utm_crs)
        except Exception:
            gdf_union = gpd.GeoDataFrame(geometry=union_series.explode(), crs=utm_crs)

        if out_path.lower().endswith(".gpkg"):
            gdf_parts.to_file(out_path, layer="shadow_parts", driver="GPKG")
            gdf_union.to_file(out_path, layer="shadow_union", driver="GPKG")
            print(f"Saved shadow layers to {out_path} (layers: shadow_parts, shadow_union)")
        else:
            base = out_path[:-8] if out_path.lower().endswith(".geojson") else out_path
            parts_path = base + "_shadow_parts.geojson"
            union_path = base + "_shadow_union.geojson"
            gdf_parts.to_file(parts_path, driver="GeoJSON")
            gdf_union.to_file(union_path, driver="GeoJSON")
            print(f"Saved {parts_path} and {union_path}")

    # 6) Shaded fraction & experienced length
    print("[6/6] Calculating shaded fractions and experienced lengths...")
    data = []
    it_sw = sidewalks_all.itertuples()
    if TQDM:
        it_sw = tqdm(it_sw, total=len(sidewalks_all), desc="    Sidewalks")
    for row in it_sw:
        geom = row.geometry
        S = shaded_fraction(geom, shadow_union) if shadow_union is not None else 0.0
        ell = float(geom.length)
        experienced = float(alpha * (1.0 - S) * ell + 1.0 * S * ell) # from CoolWalks paper
        data.append({
            "source": getattr(row, "source", "unknown"),
            "length_m": ell,
            "shade_frac": S,
            "experienced_len": experienced,
            "geometry": geom
        })

    out_gdf = gpd.GeoDataFrame(data, crs=utm_crs)
    if out_path.lower().endswith(".gpkg"):
        layer = "sidewalk_shade"
        out_gdf.to_file(out_path, layer=layer, driver="GPKG")
        print(f"Saved: {out_path} (layer='{layer}')")
    else:
        out_gdf.to_file(out_path, driver="GeoJSON")
        print(f"Saved: {out_path}")

    print("\nSummary:")
    print(f"  Segments: {len(out_gdf)}")
    print(f"  Mean shade fraction: {out_gdf['shade_frac'].mean():.3f}")
    q = np.percentile(out_gdf['shade_frac'], [25,50,75])
    print(f"  25/50/75th pct shade: {q[0]:.3f}, {q[1]:.3f}, {q[2]:.3f}")
    ratio = (out_gdf['experienced_len']/out_gdf['length_m']).replace([np.inf, -np.inf], np.nan).dropna()
    print(f"  Mean experienced_len/length ratio: {ratio.mean():.3f}")
    print("Done.")
    



# ------------------------------ Point Query Function ------------------------------

def query_point_shade(
    lat: float,
    lon: float,
    timestamp: str,
    search_radius_m: float = 200.0,
    include_trees: bool = False,
    floor_height: float = FLOOR_HEIGHT_M,
    default_bldg_h: float = DEFAULT_BUILDING_HEIGHT_M,
    default_tree_h: float = DEFAULT_TREE_HEIGHT_M,
    crown_radius: float = DEFAULT_CROWN_RADIUS_M,
) -> dict:
    """
    Fast point-in-shade query for a given (lat, lon, timestamp).

    Returns a dict with:
      - in_shade (bool): whether the point is in shadow
      - street_name (str | None): OSM name of the nearest street segment
      - street_osm_id (str | None): OSM id of the nearest street segment
      - shadow_ratio (float): fraction of the nearest street segment that is in shadow (0-1)
      - sun_azimuth (float): solar azimuth in degrees
      - sun_elevation (float): solar elevation in degrees

    Speed optimisations vs. the full pipeline:
      * Only a small bbox around the point is downloaded from OSM
      * Shadow union is computed once and reused
      * STRtree spatial index is used for nearest-segment lookup
    """
    dt_utc = to_utc_timestamp(timestamp)

    # --- 1. Determine local UTM from point ---
    zone = int((lon + 180.0) // 6.0) + 1
    utm_epsg = 32600 + zone if lat >= 0 else 32700 + zone
    utm_crs = f"EPSG:{utm_epsg}"

    # Project query point to UTM
    pt_wgs = Point(lon, lat)
    pt_gdf = gpd.GeoDataFrame(geometry=[pt_wgs], crs="EPSG:4326").to_crs(utm_crs)
    pt_utm = pt_gdf.geometry.iloc[0]

    # --- 2. Build a tight bbox around the point (in WGS84) ---
    # search_radius_m in metres -> approximate degree offset
    deg_lat = search_radius_m / 111_320.0
    deg_lon = search_radius_m / (111_320.0 * np.cos(np.deg2rad(lat)))
    north = lat + deg_lat
    south = lat - deg_lat
    east  = lon + deg_lon
    west  = lon - deg_lon
    bbox = (north, south, east, west)

    # --- 3. Sun position ---
    az, elev = get_sun(dt_utc, lat, lon)

    # --- 4. Download buildings (small bbox, fast) ---
    try:
        bldgs_raw = fetch_osm_geometries(None, bbox, tags={"building": True})
        if bldgs_raw.crs is None:
            bldgs_raw = bldgs_raw.set_crs("EPSG:4326", allow_override=True)
        bldgs_raw = bldgs_raw.to_crs(utm_crs)
        bldgs = bldgs_raw[bldgs_raw.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
        print(f"[DEBUG] bldgs fetched: {len(bldgs)} polygons")
        bldgs["H"] = [
            estimate_building_height(attrs)
            for attrs in bldgs.drop(columns="geometry").to_dict(orient="records")
        ]
    except Exception:
        bldgs = gpd.GeoDataFrame(columns=["geometry", "H"], crs=utm_crs)

    # --- 5. Compute shadow union ---
    all_shadow_geoms = []

    if elev > SUN_ELEV_MIN_DEG:
        for row in bldgs.itertuples():
            geom = row.geometry
            try:
                geom = geom.buffer(0)
            except Exception:
                pass
            sp = building_shadow(geom, getattr(row, "H", default_bldg_h), az, elev)
            if sp is not None and not sp.is_empty:
                all_shadow_geoms.append(sp)

        if include_trees:
            for tag in ({"natural": "tree"}, {"natural": "tree_row"}):
                try:
                    trees_raw = fetch_osm_geometries(None, bbox, tags=tag)
                    if trees_raw.crs is None:
                        trees_raw = trees_raw.set_crs("EPSG:4326", allow_override=True)
                    trees_raw = trees_raw.to_crs(utm_crs)
                    for geom in trees_raw.geometry:
                        sp = tree_shadow_geom(geom, default_tree_h, crown_radius, az, elev)
                        if sp is not None and not sp.is_empty:
                            all_shadow_geoms.append(sp)
                except Exception:
                    pass

    shadow_union = unary_union(all_shadow_geoms) if all_shadow_geoms else None

    # --- 6. Is the point in shade? ---
    in_shade = False
    if shadow_union is not None:
        try:
            in_shade = bool(pt_utm.within(shadow_union))
        except Exception:
            in_shade = False

    # --- 7. Nearest OSM street segment + shadow ratio ---
    street_name = None
    street_osm_id = None
    shadow_ratio = 0.0

    try:
        print(f"[DEBUG] Fetching drive graph for bbox: N={north:.5f} S={south:.5f} E={east:.5f} W={west:.5f}")
        # OSMnx 2.x: graph_from_bbox takes a single bbox tuple (left, bottom, right, top)
        G_drive = ox.graph_from_bbox((west, south, east, north), network_type="drive")
        roads_wgs = ox.graph_to_gdfs(G_drive, nodes=False, edges=True)
        print(f"[DEBUG] roads_wgs shape: {roads_wgs.shape}, CRS: {roads_wgs.crs}")
        print(f"[DEBUG] roads_wgs index names: {roads_wgs.index.names}")
        print(f"[DEBUG] roads_wgs columns: {list(roads_wgs.columns)}")

        if roads_wgs.crs is None:
            roads_wgs = roads_wgs.set_crs("EPSG:4326", allow_override=True)
        roads_utm = roads_wgs.to_crs(utm_crs)
        roads_utm = roads_utm.reset_index()
        print(f"[DEBUG] roads_utm columns after reset_index: {list(roads_utm.columns)}")

        sindex = roads_utm.sindex
        print(f"[DEBUG] pt_utm coords: {pt_utm.x:.2f}, {pt_utm.y:.2f}")
        try:
            nearest_pos = list(sindex.nearest(pt_utm, return_all=False))
            print(f"[DEBUG] nearest_pos (geometry API): {nearest_pos}")
        except TypeError as te:
            print(f"[DEBUG] geometry API failed ({te}), trying bounds fallback")
            nearest_pos = list(sindex.nearest(pt_utm.bounds, 1))
            print(f"[DEBUG] nearest_pos (bounds API): {nearest_pos}")

        if nearest_pos:
            # sindex.nearest returns a list of arrays e.g. [array([0]), array([14])]
            # Flatten to a single integer row index
            raw = nearest_pos[0]
            # row_idx = int(raw.flat[0]) if hasattr(raw, "flat") else int(raw)
            row_idx = int(nearest_pos[1].flat[0]) # The actual road index is in nearest_pos[1].
            nearest_row = roads_utm.iloc[row_idx]
            print(f"[DEBUG] nearest_row index: {row_idx}")
            print(f"[DEBUG] nearest_row keys: {list(nearest_row.index)}")
            print(f"[DEBUG] nearest_row 'name' raw value: {nearest_row.get('name', '<<missing>>')!r}")
            print(f"[DEBUG] nearest_row 'osmid' raw value: {nearest_row.get('osmid', '<<missing>>')!r}")
            print(f"[DEBUG] nearest_row 'u' raw value: {nearest_row.get('u', '<<missing>>')!r}")

            def _scalar(val):
                """Extract a plain Python scalar from a value that may be a Series, list, or nan."""
                import pandas as pd
                if isinstance(val, pd.Series):
                    val = val.iloc[0]
                if isinstance(val, list):
                    val = val[0] if val else None
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    return None
                return val

            street_name = _scalar(nearest_row.get("name", None))
            if not isinstance(street_name, str):
                street_name = None

            osmid_val = _scalar(nearest_row.get("osmid", None))
            if osmid_val is None:
                osmid_val = _scalar(nearest_row.get("u", None))
            street_osm_id = str(int(osmid_val)) if osmid_val is not None else None

            print(f"[DEBUG] resolved street_name={street_name!r}, street_osm_id={street_osm_id!r}")

            seg_geom = nearest_row.geometry
            shadow_ratio = shaded_fraction(seg_geom, shadow_union) if shadow_union is not None else 0.0
        else:
            print("[DEBUG] nearest_pos was empty — no segments found")

    except Exception as e:
        import traceback
        print(f"[DEBUG] Exception in street lookup: {e}")
        traceback.print_exc()

    return {
        "in_shade": in_shade,
        "street_name": street_name,
        "street_osm_id": street_osm_id,
        "shadow_ratio": round(float(shadow_ratio), 4),
        "sun_azimuth": round(float(az), 2),
        "sun_elevation": round(float(elev), 2),
    }

# ------------------------------ CLI ------------------------------
def main():
    ap = argparse.ArgumentParser(description="Shade-based sidewalk potential (sun vs. shade)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--place", type=str, help="Place name (e.g., 'Downtown Boston, Massachusetts, USA')")
    g.add_argument("--bbox", type=str, help="Bounding box as 'north,south,east,west'")

    ap.add_argument("--dt", type=str, required=True, help="Datetime (ISO). E.g., '2025-07-29T14:00:00-04:00'")
    ap.add_argument("--alpha", type=float, default=3.0, help="Sun-aversion parameter (penalty for sun-exposed length)")
    ap.add_argument("--include-trees", action="store_true", help="Include tree & tree_row shadows (approximate)")
    ap.add_argument("--footway-source", type=str, default="osm", choices=["osm","synth","both"],
                    help="Use OSM-mapped walk edges, synthesized sidewalks, or both")
    ap.add_argument("--out", type=str, required=True, help="Output GeoPackage (.gpkg) or GeoJSON (.geojson)")
    ap.add_argument("--floor-height", type=float, default=FLOOR_HEIGHT_M, help="Meters per building level if height missing")
    ap.add_argument("--default-bldg-h", type=float, default=DEFAULT_BUILDING_HEIGHT_M, help="Default building height (m)")
    ap.add_argument("--default-tree-h", type=float, default=DEFAULT_TREE_HEIGHT_M, help="Default tree height (m)")
    ap.add_argument("--crown-radius", type=float, default=DEFAULT_CROWN_RADIUS_M, help="Default crown radius (m)")
    ap.add_argument("--sidewalk-offset", type=float, default=SIDEWALK_OFFSET_M, help="Offset for synthesized sidewalks (m)")
    ap.add_argument("--save-shadows", action="store_true", help="Also export building/tree shadow polygons for QGIS") #Shadow polygons
    
    ap.add_argument("--detectree-ortho", type=str, help="Path to orthophoto (GeoTIFF/COG) for tree segmentation")
    ap.add_argument("--detectree-min-area", type=float, default=3.0, help="Min canopy polygon area (m²)")
    ap.add_argument("--detectree-smooth", type=int, default=1, help="Binary opening/closing iterations")

    ap.add_argument("--auto-ortho", action="store_true", help="Fetch latest NAIP (US) or Sentinel-2 (global) tiles automatically")
    ap.add_argument("--ortho-source", default="naip", choices=["naip", "sentinel"], help="Which open imagery source to pull when --auto-ortho is set")

    args = ap.parse_args()

    place = args.place if args.place else None
    bbox = parse_bbox(args.bbox) if args.bbox else None
    dt_utc = to_utc_timestamp(args.dt)

    try:
        run_pipeline(place=place,
                     bbox=bbox,
                     dt_utc=dt_utc,
                     alpha=args.alpha,
                     include_trees=args.include_trees,
                     footway_source=args.footway_source,
                     out_path=args.out,
                     floor_height=args.floor_height,
                     default_bldg_h=args.default_bldg_h,
                     default_tree_h=args.default_tree_h,
                     crown_radius=args.crown_radius,
                     sidewalk_offset_m=args.sidewalk_offset,
                     save_shadows=args.save_shadows,
                     detectree_ortho=args.detectree_ortho,
                     detectree_min_area=args.detectree_min_area,
                     detectree_smooth=args.detectree_smooth,
                     auto_ortho=args.auto_ortho,
                     ortho_source=args.ortho_source)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

# if __name__ == "__main__":
#     main()

