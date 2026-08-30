#!/usr/bin/env python3
"""
Build data/austin_parks.geojson — City of Austin parkland boundary polygons
used to resolve 311 park-maintenance complaint coordinates to real park names.

Downloads the official PARD parkland boundaries (Socrata mirror v8hw-gz65 of
the ArcGIS BOUNDARIES_city_of_austin_parks layer), strips the 56 property
columns down to the park name, and writes a committed FeatureCollection so CI
never depends on a live download.

Sources:
  1. data.austintexas.gov Socrata mirror (prefer — already WGS84 GeoJSON):
     https://data.austintexas.gov/resource/v8hw-gz65.geojson
  2. City of Austin ArcGIS FeatureServer (fallback — same data, needs outSR=4326):
     https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/services/
     BOUNDARIES_city_of_austin_parks/FeatureServer/0/query
     ?where=1%3D1&outFields=OBJECTID,LOCATION_NAME&outSR=4326&f=geojson

Run:  python scripts/fetch_austin_parks.py [local_geojson_path]
Output: data/austin_parks.geojson
"""
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "austin_parks.geojson"

SOCRATA_URL = "https://data.austintexas.gov/resource/v8hw-gz65.geojson?$limit=50000"
ARCGIS_URL = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/services/"
    "BOUNDARIES_city_of_austin_parks/FeatureServer/0/query"
    "?where=1%3D1&outFields=OBJECTID,LOCATION_NAME&outSR=4326&f=geojson"
)


def _download(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "austin311-parks/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    data: dict = None
    src = "socrata"

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
            data = _download(SOCRATA_URL)
        except Exception as e:
            print(f"Socrata download failed ({e}); trying ArcGIS…")
            try:
                data = _download(ARCGIS_URL)
                src = "arcgis"
            except Exception as e2:
                print(f"ArcGIS fallback failed too: {e2}")
                sys.exit(1)

    features = data.get("features", [])
    print(f"Source {src}: {len(features)} parkland features")

    kept = []
    dropped = 0
    seen = set()
    for f in features:
        props = f.get("properties", {}) or {}
        # Socrata mirror uses location_name; ArcGIS uses LOCATION_NAME.
        name = str(props.get("location_name") or props.get("LOCATION_NAME") or "").strip()
        if not name:
            dropped += 1
            continue
        geometry = f.get("geometry")
        if not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
            dropped += 1
            continue
        key = (name, json.dumps(geometry.get("coordinates"), sort_keys=True))
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append({
            "type": "Feature",
            "properties": {"location_name": name},
            "geometry": geometry,
        })

    kept.sort(key=lambda f: f["properties"]["location_name"].lower())

    out = {
        "type": "FeatureCollection",
        "name": "austin_parks",
        "source": f"{src} · City of Austin PARD parkland boundaries (v8hw-gz65)",
        "features": kept,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out), encoding="utf-8")
    print(f"Kept {len(kept)} park parcels, dropped {dropped}")
    names = sorted({f["properties"]["location_name"] for f in kept})
    print(f"Distinct park names: {len(names)}")
    print(f"Sample: {', '.join(names[:12])} …")
    print(f"Wrote {OUT_PATH.stat().st_size:,} bytes to {OUT_PATH}")


if __name__ == "__main__":
    main()
