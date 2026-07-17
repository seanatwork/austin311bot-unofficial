"""
Point-in-polygon geolocation for Austin council districts.

Uses the City of Austin ArcGIS council district GeoJSON to determine
which district (1-10) a latitude/longitude point falls in.
"""

import json
import logging
from typing import Optional, Dict, Tuple

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
