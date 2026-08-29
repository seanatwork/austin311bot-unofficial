#!/usr/bin/env python3
"""
One-off fetcher: build data/austin_zctas.geojson — trimmed Austin-area ZIP
boundaries (US Census ZCTA5) used by the "311 Wrapped" per-ZIP page.

Downloads the Texas ZCTA5 GeoJSON, keeps only features whose bounding box
intersects the Austin MSA box, strips properties down to the ZIP code, and
writes a committed FeatureCollection to data/austin_zctas.geojson so CI never
depends on a live download.

Sources (tried in order):
  1. OpenDataDE State-zip-code-GeoJSON mirror of US Census ZCTA5
     https://raw.githubusercontent.com/OpenDataDE/State-zip-code-GeoJSON/master/tx_texas_zip_codes_geo.min.json
  2. US Census TIGER/Line ZCTA5 (2023) shapefile zipped
     https://www2.census.gov/geo/tiger/TIGER2023/ZCTA520/tl_2023_us_zcta520.zip
     (only used if the mirror fails; requires unzip on PATH)

Run:  python scripts/fetch_austin_zctas.py [local_tx_geojson_path]
Output: data/austin_zctas.geojson
"""
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "austin_zctas.geojson"

MIRROR_URL = (
    "https://raw.githubusercontent.com/OpenDataDE/State-zip-code-GeoJSON/"
    "master/tx_texas_zip_codes_geo.min.json"
)
CENSUS_URL = "https://www2.census.gov/geo/tiger/TIGER2023/ZCTA520/tl_2023_us_zcta520.zip"

# Austin MSA box (slightly wider than the map filter bbox to catch edge ZIPs).
BBOX = {"min_lat": 29.9, "max_lat": 30.7, "min_lon": -98.3, "max_lon": -97.2}


def _feature_bbox(feature: dict) -> tuple:
    """Return (min_lon, min_lat, max_lon, max_lat) for a feature's geometry."""
    geom = feature.get("geometry", {})
    coords = geom.get("coordinates", [])
    if not coords:
        return None
    # Walk all rings to a flat list of [lon, lat]
    flat = []
    stack = list(coords)
    while stack:
        item = stack.pop()
        if item and isinstance(item[0], (int, float)):
            flat.append(item)
        else:
            stack.extend(item)
    if not flat:
        return None
    lons = [p[0] for p in flat]
    lats = [p[1] for p in flat]
    return (min(lons), min(lats), max(lons), max(lats))


def _bbox_overlaps(b: tuple, bbox: dict) -> bool:
    if b is None:
        return False
    min_lon, min_lat, max_lon, max_lat = b
    return not (
        max_lon < bbox["min_lon"]
        or min_lon > bbox["max_lon"]
        or max_lat < bbox["min_lat"]
        or min_lat > bbox["max_lat"]
    )


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "austin311-wrapped/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _load_from_mirror() -> dict:
    raw = _download(MIRROR_URL)
    return json.loads(raw)


def _load_from_census() -> dict:
    """Fetch the TIGER/Line ZCTA5 shapefile and convert via built-in tools.

    The zip contains a .shp + .dbf + .shx. We cannot read SHP with the stdlib,
    so this falls back to downloading the census CARTO-style GeoJSON endpoint
    if available (no shapefile parsing).
    """
    # Fall back to the Census API GeoJSON (ZCTA5 2020) as a pure-JSON path.
    census_json_url = (
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "ZCTA520/MapServer/0/query?where=STATE='48'&outFields=ZCTA5CE20&f=geojson"
    )
    raw = _download(census_json_url)
    return json.loads(raw)


def main() -> None:
    data: dict = None
    src = "mirror"

    if len(sys.argv) > 1:
        local = Path(sys.argv[1])
        if local.exists():
            data = json.loads(local.read_text())
            src = f"local:{local.name}"
        else:
            print(f"Local file not found: {local}")
            sys.exit(1)
    else:
        try:
            data = _load_from_mirror()
        except Exception as e:
            print(f"Mirror download failed ({e}); trying Census…")
            try:
                data = _load_from_census()
                src = "census"
            except Exception as e2:
                print(f"Census fallback failed too: {e2}")
                sys.exit(1)

    features = data.get("features", [])
    print(f"Source {src}: {len(features)} Texas features")

    # Census (mirror) uses ZCTA5CE10; Census API uses ZCTA5CE20.
    kept = []
    dropped = 0
    for f in features:
        props = f.get("properties", {}) or {}
        zip_code = str(props.get("ZCTA5CE10") or props.get("ZCTA5CE20") or "").strip()
        if not zip_code or not zip_code.isdigit():
            dropped += 1
            continue
        if len(zip_code) != 5:
            dropped += 1
            continue
        bbox = _feature_bbox(f)
        if not _bbox_overlaps(bbox, BBOX):
            dropped += 1
            continue
        kept.append({
            "type": "Feature",
            "properties": {"zip": zip_code},
            "geometry": f.get("geometry"),
        })

    kept.sort(key=lambda f: f["properties"]["zip"])

    out = {
        "type": "FeatureCollection",
        "name": "austin_zctas",
        "source": f"{src} · trimmed to Austin MSA bbox",
        "features": kept,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out), encoding="utf-8")
    print(f"Kept {len(kept)} Austin-area ZIPs, dropped {dropped}")
    print(f"ZIPs: {', '.join(f['properties']['zip'] for f in kept[:12])} …")
    print(f"Wrote {OUT_PATH.stat().st_size:,} bytes to {OUT_PATH}")


if __name__ == "__main__":
    main()
