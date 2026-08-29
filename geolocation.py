"""
Point-in-polygon geolocation for Austin council districts.

Uses the City of Austin ArcGIS council district GeoJSON to determine
which district (1-10) a latitude/longitude point falls in.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import requests

logger = logging.getLogger(__name__)

# Austin council district polygons from City of Austin ArcGIS
DISTRICTS_GEOJSON_URL = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/services/"
    "Council_Districts/FeatureServer/0/query"
    "?where=1%3D1&outFields=COUNCIL_DI&f=geojson"
)

# Cached district polygons (loaded once, reused)
_district_polygons: Optional[Dict[int, list]] = None
_district_geojson: Optional[dict] = None


def _load_districts() -> Dict[int, list]:
    """Load council district polygons from ArcGIS, returning {district_num: polygon_coords}."""
    global _district_polygons, _district_geojson

    if _district_polygons is not None:
        return _district_polygons

    logger.info("Loading council district GeoJSON from ArcGIS...")
    try:
        resp = requests.get(DISTRICTS_GEOJSON_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        _district_geojson = data
    except Exception:
        logger.warning("Could not fetch district GeoJSON — geolocation disabled")
        _district_polygons = {}
        return _district_polygons

    polygons: Dict[int, list] = {}

    for feature in data.get("features", []):
        props = feature.get("properties", {})
        district_num = int(props.get("COUNCIL_DI", 0))
        if district_num < 1 or district_num > 10:
            continue

        geometry = feature.get("geometry", {})
        coords = _extract_coords(geometry)

        if coords:
            polygons[district_num] = coords

    _district_polygons = polygons
    logger.info(f"Loaded {len(polygons)} council district polygons")
    return polygons


def _extract_coords(geometry: dict) -> list:
    """Extract coordinate ring(s) from a GeoJSON geometry object.

    Handles Polygon and MultiPolygon types. Returns the first (outer) ring
    for point-in-polygon testing.
    """
    geom_type = geometry.get("type", "")

    if geom_type == "Polygon":
        rings = geometry.get("coordinates", [])
        return rings[0] if rings else []

    if geom_type == "MultiPolygon":
        polygons = geometry.get("coordinates", [])
        # Use the largest polygon (by coordinate count) as the main ring
        best: list = []
        for poly in polygons:
            if poly and len(poly[0]) > len(best):
                best = poly[0]
        return best

    return []


def _point_in_polygon(lat: float, lon: float, polygon: list) -> bool:
    """Ray-casting algorithm — determines if a point is inside a polygon.

    polygon: list of [lon, lat] pairs (GeoJSON order).
    """
    n = len(polygon)
    inside = False
    j = n - 1

    for i in range(n):
        lng_i, lat_i = polygon[i]
        lng_j, lat_j = polygon[j]

        if ((lat_i > lat) != (lat_j > lat)) and (
            lon < (lng_j - lng_i) * (lat - lat_i) / (lat_j - lat_i) + lng_i
        ):
            inside = not inside
        j = i

    return inside


def point_in_district(lat: float, lon: float) -> Optional[int]:
    """Return the Austin council district number (1-10) for a lat/lon point.

    Returns None if the point is outside all districts or geolocation is unavailable.
    """
    if not (-98.5 < lon < -97.0 and 30.0 < lat < 30.7):
        # Outside Austin bounding box — no district
        return None

    districts = _load_districts()
    if not districts:
        return None

    for district_num, polygon in districts.items():
        if _point_in_polygon(lat, lon, polygon):
            return district_num

    return None


def get_district_geojson() -> Optional[dict]:
    """Return the raw council district GeoJSON (cached)."""
    global _district_geojson
    if _district_geojson is None:
        _load_districts()
    return _district_geojson


def get_district_label(district_num: int) -> str:
    """Return a human-readable label for a council district number."""
    return f"District {district_num}"


# ── ZIP code (ZCTA5) lookup ─────────────────────────────────────────────────
# Backed by data/austin_zctas.geojson (committed, trimmed to the Austin MSA),
# built by scripts/fetch_austin_zctas.py. No runtime HTTP.

# ZIP → well-known area label (optional; only central/notable ZIPs)
ZIP_NAMES: Dict[str, str] = {
    "78701": "Downtown",
    "78702": "East Austin",
    "78703": "West Austin",
    "78704": "South Congress / Zilker",
    "78705": "West Campus",
    "78721": "East Austin",
    "78722": "North University",
    "78723": "Mueller",
    "78731": "Allandale",
    "78741": "East Riverside",
    "78745": "South Austin",
    "78748": "South Austin",
    "78751": "Hyde Park",
    "78752": "North Loop",
    "78753": "North Austin",
    "78754": "Northeast Austin",
    "78756": "Crestview",
    "78757": "North Shoal Creek",
    "78758": "North Austin",
    "78759": "Arboretum",
    "78653": "Manor",
    "78660": "Pflugerville",
    "78664": "Round Rock",
    "78681": "Round Rock",
}

_ZIP_DATA: Optional[List[Tuple[str, list, tuple]]] = None  # (zip, ring, bbox)
_ZIP_BBOX = (-98.3, 29.9, -97.2, 30.7)  # (min_lon, min_lat, max_lon, max_lat)


def _all_outer_rings(geometry: dict) -> List[list]:
    """Return every outer ring for a Polygon/MultiPolygon geometry."""
    geom_type = geometry.get("type", "")
    coords = geometry.get("coordinates", []) or []
    rings: List[list] = []
    if geom_type == "Polygon":
        if coords:
            rings.append(coords[0])
    elif geom_type == "MultiPolygon":
        for poly in coords:
            if poly:
                rings.append(poly[0])
    return rings


def _ring_bbox(ring: list) -> tuple:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return (min(lons), min(lats), max(lons), max(lats))


def load_zips() -> List[Tuple[str, list, tuple]]:
    """Load Austin ZIP polygons from the committed GeoJSON.

    Returns a list of (zip_code, outer_ring, bbox) tuples.
    """
    global _ZIP_DATA
    if _ZIP_DATA is not None:
        return _ZIP_DATA

    path = Path(__file__).resolve().parent / "data" / "austin_zctas.geojson"
    if not path.exists():
        logger.warning("Missing data/austin_zctas.geojson — ZIP lookup disabled")
        _ZIP_DATA = []
        return _ZIP_DATA

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not parse ZIP GeoJSON — {e}")
        _ZIP_DATA = []
        return _ZIP_DATA

    _ZIP_DATA = []
    for feature in data.get("features", []):
        props = feature.get("properties", {}) or {}
        zip_code = str(props.get("zip") or "").strip()
        if not zip_code:
            continue
        for ring in _all_outer_rings(feature.get("geometry", {})):
            if len(ring) < 4:
                continue
            _ZIP_DATA.append((zip_code, ring, _ring_bbox(ring)))
    logger.info(f"Loaded {len(_ZIP_DATA)} ZIP polygon rings")
    return _ZIP_DATA


def zip_for_point(lat: float, lon: float) -> Optional[str]:
    """Return the Austin ZIP (ZCTA5) containing a lat/lon point.

    Returns None if the point is outside all loaded ZIP polygons or ZIP data
    is unavailable.
    """
    min_lon, min_lat, max_lon, max_lat = _ZIP_BBOX
    if not (min_lon < lon < max_lon and min_lat < lat < max_lat):
        return None

    for zip_code, ring, (r_min_lon, r_min_lat, r_max_lon, r_max_lat) in load_zips():
        if not (r_min_lon <= lon <= r_max_lon and r_min_lat <= lat <= r_max_lat):
            continue
        if _point_in_polygon(lat, lon, ring):
            return zip_code
    return None


def get_zip_label(zip_code: str) -> str:
    """Return a human-readable area label for a ZIP, or the ZIP itself."""
    return ZIP_NAMES.get(zip_code, zip_code)
