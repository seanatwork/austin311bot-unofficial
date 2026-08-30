#!/usr/bin/env python3
"""
Generate static Folium maps for 311 service categories.

Run locally:
    python scripts/generate_map.py bicycle
    python scripts/generate_map.py graffiti
    python scripts/generate_map.py homeless

Run in CI (GitHub Actions) with AUSTINAPIKEY set for higher rate limits.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()


def format_central_time() -> str:
    """Return current time formatted in US Central Time (CDT/CST)."""
    from datetime import datetime, timezone
    import time
    
    # Get current UTC time
    utc_now = datetime.now(timezone.utc)
    
    # Convert to Central Time (UTC-5 for CDT, UTC-6 for CST)
    # Check if DST is in effect (rough approximation: March to November)
    month = utc_now.month
    is_dst = 3 <= month <= 11  # Simplified DST check
    offset_hours = -5 if is_dst else -6
    
    # Calculate Central Time
    from datetime import timedelta
    central_now = utc_now + timedelta(hours=offset_hours)
    
    tz_abbr = "CDT" if is_dst else "CST"
    return central_now.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")


GA_SNIPPET = """<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-TS158R7XSN"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-TS158R7XSN');
</script>
"""


def generate_bicycle_map(days_back: int = 90) -> tuple:
    """Generate bicycle infrastructure map."""
    from bicycle.bicycle_bot import generate_bicycle_map
    return generate_bicycle_map(days_back)


def generate_graffiti_map(days_back: int = 90) -> tuple:
    """Generate graffiti abatement map."""
    from graffiti.graffiti_bot import generate_graffiti_map
    return generate_graffiti_map(days_back)


def generate_homeless_map(days_back: int = 180) -> tuple:
    """Generate homeless encampment map."""
    from homeless.homeless_bot import generate_encampment_map
    return generate_encampment_map(days_back)


def generate_traffic_map(days_back: int = 90) -> tuple:
    """Generate traffic & infrastructure map."""
    from infrastructureandtransportation.traffic_bot import generate_traffic_map
    return generate_traffic_map(days_back)


def generate_parking_map(days_back: int = 90) -> tuple:
    """Generate parking enforcement map."""
    from parking.parking_bot import generate_parking_map
    return generate_parking_map(days_back)


def generate_parking_trends_page(days_back: int = 365) -> tuple:
    """Generate parking complaints trends page."""
    from parking.trends import generate_parking_trends
    return generate_parking_trends(days_back)


def generate_graffiti_trends_page(days_back: int = 365) -> tuple:
    """Generate graffiti abatement trends page."""
    from graffiti.trends import generate_graffiti_trends
    return generate_graffiti_trends(days_back)


def generate_crime_trends_page(days_back: int = 365) -> tuple:
    """Generate APD crime trends page."""
    from crime.trends import generate_crime_trends
    return generate_crime_trends(days_back)


def generate_hate_crime_page(days_back: int = 0) -> tuple:
    """Generate APD hate crime incidents page."""
    from crime.hate_crime import generate_hate_crime
    return generate_hate_crime()


def generate_noise_trends_page(days_back: int = 365) -> tuple:
    """Generate noise complaint trends page."""
    from noisecomplaints.trends import generate_noise_trends
    return generate_noise_trends(days_back)


def generate_crime_map(days_back: int = 90) -> tuple:
    """Generate APD crime choropleth map by council district."""
    from crime.crime_map import generate_crime_map
    return generate_crime_map(days_back)


def generate_noise_map(days_back: int = 90) -> tuple:
    """Generate noise complaints point map."""
    from noisecomplaints.noise_bot import generate_noise_map
    return generate_noise_map(days_back)


def generate_parks_map(days_back: int = 90) -> tuple:
    """Generate park maintenance point map."""
    from parks.parks_bot import generate_parks_map
    return generate_parks_map(days_back)


def generate_parks_hub(days_back: int = 90) -> tuple:
    """Generate searchable parks hub landing page."""
    from parks.parks_bot import generate_parks_hub
    return generate_parks_hub(days_back)


def generate_water_map(days_back: int = 90) -> tuple:
    """Generate water conservation violations point map."""
    from waterconservation.water_conservation_bot import generate_water_map
    return generate_water_map(days_back)


def generate_childcare_map(days_back: int = 90) -> tuple:
    """Generate childcare facility compliance map."""
    from childcare.childcare_bot import generate_childcare_map
    return generate_childcare_map(days_back)


def generate_animal_map(days_back: int = 90) -> tuple:
    """Generate animal services point map."""
    from animalsvc.animal_bot import generate_animal_map
    return generate_animal_map(days_back)


def generate_dead_animal_map(days_back: int = 90) -> tuple:
    """Generate dead animal collection point map."""
    from animalsvc.dead_animal_bot import generate_dead_animal_map
    return generate_dead_animal_map(days_back)


def generate_budget_page(days_back: int = 365) -> tuple:
    """Generate Austin budget page."""
    import io
    from scripts.generate_budget import main as generate_budget_main, OUT_PATH
    try:
        generate_budget_main()
        buf = io.BytesIO(OUT_PATH.read_bytes())
        return buf, "Budget page generated successfully"
    except Exception as e:
        return None, f"Budget generation failed: {e}"


def generate_homeless_trends_page(days_back: int = 365) -> tuple:
    """Generate homeless 311 trends page."""
    from homeless.trends import generate_homeless_trends
    return generate_homeless_trends(days_back)


def generate_storm_map(days_back: int = 90) -> tuple:
    """Generate storm debris, drainage & flooding point map."""
    from storm.storm_bot import generate_storm_map
    return generate_storm_map(days_back)


def generate_storm_trends_page(days_back: int = 365) -> tuple:
    from storm.trends import generate_storm_trends
    return generate_storm_trends(days_back)


def generate_parks_trends_page(days_back: int = 365) -> tuple:
    from parks.trends import generate_parks_trends
    return generate_parks_trends(days_back)


def generate_animal_trends_page(days_back: int = 365) -> tuple:
    from animalsvc.trends import generate_animal_trends
    return generate_animal_trends(days_back)


def generate_dead_animal_trends_page(days_back: int = 365) -> tuple:
    from animalsvc.dead_animal_trends import generate_dead_animal_trends
    return generate_dead_animal_trends(days_back)


def generate_tree_map(days_back: int = 90) -> tuple:
    """Generate urban trees & canopy point map."""
    from trees.trees_bot import generate_tree_map
    return generate_tree_map(days_back)


def generate_cameras_map(days_back: int = 90) -> tuple:
    """Generate live traffic cameras map."""
    from trafficcameras.cameras_bot import generate_cameras_map
    return generate_cameras_map(days_back)


def generate_shelter_page(days_back: int = 365) -> tuple:
    """Generate Animal Center shelter data cache (non-map dashboard)."""
    import io
    from scripts.generate_shelter_data import main as shelter_main, OUT as SHELTER_OUT
    try:
        shelter_main()
        buf = io.BytesIO(SHELTER_OUT.read_bytes())
        return buf, "Shelter data generated successfully"
    except Exception as e:
        return None, f"Shelter data generation failed: {e}"


def generate_nearby_page(days_back: int = 180) -> tuple:
    """Generate "311 Near You" dynamic map page with embedded request data."""
    import io
    from scripts.generate_nearby_page import main as nearby_main
    try:
        nearby_main()
        out_path = Path("docs") / "nearby" / "index.html"
        buf = io.BytesIO(out_path.read_bytes())
        return buf, "Nearby page generated successfully"
    except Exception as e:
        return None, f"Nearby generation failed: {e}"


CATEGORY_MAPS = {
    "bicycle": (generate_bicycle_map, "bicycle/index.html"),
    "graffiti": (generate_graffiti_map, "graffiti/index.html"),
    "homeless": (generate_homeless_map, "homeless/index.html"),
    "traffic": (generate_traffic_map, "traffic/index.html"),
    "parking": (generate_parking_map, "parking/index.html"),
    "parking-trends": (generate_parking_trends_page, "parking/trends/index.html"),
    "crime": (generate_crime_map, "crime/index.html"),
    "crime-trends": (generate_crime_trends_page, "crime/trends/index.html"),
    "hate-crime": (generate_hate_crime_page, "crime/hatecrime/index.html"),
    "noise": (generate_noise_map, "noise/index.html"),
    "noise-trends": (generate_noise_trends_page, "noise/trends/index.html"),
    "graffiti-trends": (generate_graffiti_trends_page, "graffiti/trends/index.html"),
    "parks": (generate_parks_map, "parks/map/index.html"),
    "parks-hub": (generate_parks_hub, "parks/index.html"),
    "water": (generate_water_map, "water/index.html"),
    "childcare": (generate_childcare_map, "childcare/index.html"),
    "animal": (generate_animal_map, "animal/index.html"),
    "dead-animal": (generate_dead_animal_map, "animal/dead/index.html"),
    "homeless-trends": (generate_homeless_trends_page, "homeless/trends/index.html"),
    "storm-trends": (generate_storm_trends_page, "storm/trends/index.html"),
    "parks-trends": (generate_parks_trends_page, "parks/trends/index.html"),
    "animal-trends": (generate_animal_trends_page, "animal/trends/index.html"),
    "dead-animal-trends": (generate_dead_animal_trends_page, "animal/dead/trends/index.html"),
    "budget": (generate_budget_page, "budget/index.html"),
    "nearby": (generate_nearby_page, "nearby/index.html"),
    "storm": (generate_storm_map, "storm/index.html"),
    "trees": (generate_tree_map, "trees/index.html"),
    "cameras": (generate_cameras_map, "cameras/index.html"),
    "shelter": (generate_shelter_page, "shelter/data.json"),
}


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/generate_map.py <category>")
        print(f"Available categories: {', '.join(CATEGORY_MAPS.keys())}")
        sys.exit(1)

    category = sys.argv[1].lower()
    if category not in CATEGORY_MAPS:
        print(f"Unknown category: {category}")
        print(f"Available categories: {', '.join(CATEGORY_MAPS.keys())}")
        sys.exit(1)

    generator_func, output_path = CATEGORY_MAPS[category]
    if category == "traffic":
        days_back = 90
    elif category == "homeless":
        days_back = 180
    elif category.endswith("-trends"):
        days_back = 365
    else:
        days_back = 90

    print(f"Generating {category} map (last {days_back} days)...")
    buf, summary = generator_func(days_back)

    if not buf:
        print(f"Map generation failed: {summary}")
        sys.exit(1)

    now_str = format_central_time()
    last_ran_span = (
        f'<span style="font-size: 11px; color: #888;">Last ran: {now_str}</span><br/>\n        '
    )
    html = buf.getvalue().decode("utf-8").replace(
        '<span id="map-summary"',
        last_ran_span + '<span id="map-summary"',
        1,
    )
    html = html.replace("</head>", GA_SNIPPET + "</head>", 1)

    out = Path("docs") / output_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Written {out.stat().st_size:,} bytes to {out}")
    print(summary)


if __name__ == "__main__":
    main()
