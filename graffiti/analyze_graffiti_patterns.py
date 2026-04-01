#!/usr/bin/env python3
"""
Analyze graffiti data patterns to inform bot development
"""

import sqlite3
import re
from collections import Counter, defaultdict
from datetime import datetime

def analyze_graffiti_patterns(db_path="../311_categories.db"):
    """Analyze graffiti data for bot feature insights"""
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("🎨 Graffiti Data Pattern Analysis")
    print("=" * 50)
    
    # Get all graffiti records
    cursor.execute("""
        SELECT service_request_id, requested_datetime, updated_datetime, 
               status, status_notes, address, zipcode, lat, long, 
               media_url, attributes_json, extended_attributes_json
        FROM open311_requests
        WHERE service_code = 'HHSGRAFF'
        ORDER BY requested_datetime DESC
    """)
    
    records = cursor.fetchall()
    print(f"📊 Total records analyzed: {len(records):,}")
    
    # Status analysis
    statuses = [record[3] for record in records]
    status_counts = Counter(statuses)
    print(f"\n📋 Status Distribution:")
    for status, count in status_counts.most_common():
        print(f"   {status}: {count:,} ({count/len(records)*100:.1f}%)")
    
    # Address patterns
    addresses = [record[5] for record in records if record[5]]
    address_patterns = analyze_addresses(addresses)
    
    # Status notes analysis
    status_notes = [record[4] for record in records if record[4]]
    notes_analysis = analyze_status_notes(status_notes)
    
    # Temporal patterns
    dates = [record[1] for record in records]
    temporal_analysis = analyze_temporal_patterns(dates)
    
    # Geographic analysis
    locations = [(record[7], record[8]) for record in records if record[7] and record[8]]
    geo_analysis = analyze_geographic_patterns(locations)
    
    # Media analysis
    media_urls = [record[9] for record in records if record[9]]
    print(f"\n📸 Media attachments: {len(media_urls)} ({len(media_urls)/len(records)*100:.1f}%)")
    
    # Attributes analysis
    attributes = [record[10] for record in records if record[10]]
    attr_analysis = analyze_attributes(attributes)
    
    # Bot feature recommendations
    print(f"\n🤖 BOT FEATURE RECOMMENDATIONS")
    print("=" * 50)
    
    recommendations = generate_bot_recommendations(
        status_counts, address_patterns, notes_analysis, 
        temporal_analysis, geo_analysis, attr_analysis
    )
    
    for i, rec in enumerate(recommendations, 1):
        print(f"{i}. {rec}")
    
    conn.close()

def analyze_addresses(addresses):
    """Analyze address patterns"""
    print(f"\n📍 Address Analysis ({len(addresses)} with addresses):")
    
    # Intersection patterns
    intersections = [addr for addr in addresses if '&' in addr or 'and' in addr.lower()]
    print(f"   Intersections: {len(intersections)} ({len(intersections)/len(addresses)*100:.1f}%)")
    
    # Street name patterns
    street_words = []
    for addr in addresses:
        words = re.findall(r'\b\w+\b', addr.lower())
        street_words.extend([word for word in words if len(word) > 2])
    
    common_streets = Counter(street_words).most_common(10)
    print(f"   Top street words: {[word for word, count in common_streets[:5]]}")
    
    return {
        'intersections': intersections,
        'common_streets': common_streets,
        'total_with_addresses': len(addresses)
    }

def analyze_status_notes(notes):
    """Analyze status notes for patterns"""
    print(f"\n📝 Status Notes Analysis ({len(notes)} with notes):")
    
    # Common keywords
    all_text = ' '.join(notes).lower()
    words = re.findall(r'\b\w+\b', all_text)
    common_words = Counter(words).most_common(20)
    
    # Action words
    action_patterns = {
        'assigned': ['assigned', 'referred', 'forwarded'],
        'in_progress': ['in progress', 'working', 'investigating'],
        'completed': ['completed', 'resolved', 'closed', 'removed'],
        'scheduled': ['scheduled', 'planned', 'expected'],
        'citizen_update': ['citizen', 'resident', 'caller']
    }
    
    pattern_counts = {}
    for pattern_name, keywords in action_patterns.items():
        count = sum(1 for note in notes if any(keyword in note.lower() for keyword in keywords))
        pattern_counts[pattern_name] = count
        if count > 0:
            print(f"   {pattern_name}: {count} mentions")
    
    # Priority indicators
    priority_keywords = ['urgent', 'priority', 'emergency', 'hazard', 'safety']
    priority_count = sum(1 for note in notes if any(keyword in note.lower() for keyword in priority_keywords))
    print(f"   Priority indicators: {priority_count}")
    
    return {
        'common_words': common_words,
        'action_patterns': pattern_counts,
        'priority_indicators': priority_count
    }

def analyze_temporal_patterns(dates):
    """Analyze temporal patterns"""
    print(f"\n⏰ Temporal Analysis:")
    
    # Convert to datetime objects
    datetimes = []
    for date_str in dates:
        try:
            if date_str.endswith('Z'):
                date_str = date_str[:-1] + '+00:00'
            dt = datetime.fromisoformat(date_str)
            datetimes.append(dt)
        except:
            continue
    
    if not datetimes:
        return {}
    
    # Day of week analysis
    weekday_counts = Counter(dt.weekday() for dt in datetimes)
    print(f"   Busiest day: {max(weekday_counts, key=weekday_counts.get)} (0=Mon, 6=Sun)")
    
    # Hour of day analysis
    hour_counts = Counter(dt.hour for dt in datetimes)
    peak_hour = max(hour_counts, key=hour_counts.get)
    print(f"   Peak hour: {peak_hour:02d}:00")
    
    # Recent vs older
    recent = sum(1 for dt in datetimes if (datetime.now(dt.tzinfo) - dt).days <= 7)
    print(f"   Last 7 days: {recent} reports")
    
    return {
        'weekday_distribution': dict(weekday_counts),
        'hour_distribution': dict(hour_counts),
        'peak_hour': peak_hour,
        'recent_7_days': recent
    }

def analyze_geographic_patterns(locations):
    """Analyze geographic patterns"""
    print(f"\n🗺️ Geographic Analysis ({len(locations)} with coordinates):")
    
    if len(locations) < 2:
        return {}
    
    # Basic clustering check
    lats = [loc[0] for loc in locations]
    lons = [loc[1] for loc in locations]
    
    lat_range = max(lats) - min(lats)
    lon_range = max(lons) - min(lons)
    
    print(f"   Latitude range: {lat_range:.4f}°")
    print(f"   Longitude range: {lon_range:.4f}°")
    print(f"   Geographic spread: {'Wide' if lat_range > 0.1 or lon_range > 0.1 else 'Concentrated'}")
    
    return {
        'total_locations': len(locations),
        'lat_range': lat_range,
        'lon_range': lon_range,
        'spread': 'wide' if lat_range > 0.1 or lon_range > 0.1 else 'concentrated'
    }

def analyze_attributes(attributes):
    """Analyze JSON attributes for additional data"""
    print(f"\n🔧 Attributes Analysis ({len(attributes)} with attributes):")
    
    # Parse JSON and look for useful fields
    parsed_attrs = []
    for attr in attributes:
        try:
            import json
            parsed = json.loads(attr)
            parsed_attrs.append(parsed)
        except:
            continue
    
    if parsed_attrs:
        # Look for common keys
        all_keys = set()
        for attr in parsed_attrs:
            if isinstance(attr, dict):
                all_keys.update(attr.keys())
            elif isinstance(attr, list):
                for item in attr:
                    if isinstance(item, dict):
                        all_keys.update(item.keys())
        
        print(f"   Common attribute keys: {list(all_keys)[:5]}")
        
        # Look for priority or categorization fields
        priority_keys = ['priority', 'urgency', 'severity', 'type']
        found_priorities = [key for key in priority_keys if key in all_keys]
        if found_priorities:
            print(f"   Priority fields found: {found_priorities}")
    
    return {
        'parsed_count': len(parsed_attrs),
        'common_keys': list(all_keys) if parsed_attrs else []
    }

def generate_bot_recommendations(status_counts, address_patterns, notes_analysis, 
                                temporal_analysis, geo_analysis, attr_analysis):
    """Generate specific bot feature recommendations"""
    
    recommendations = []
    
    # Status-based features
    open_count = status_counts.get('open', 0)
    total_count = sum(status_counts.values())
    if open_count / total_count > 0.5:
        recommendations.append("🔄 Real-time status tracking - Most reports are still open")
    
    # Location features
    if address_patterns['intersections']:
        recommendations.append("🗺️ Intersection-based reporting - Many reports at intersections")
    
    # Communication features
    if notes_analysis['action_patterns']:
        recommendations.append("📢 Automated status updates - Clear workflow patterns detected")
    
    # Temporal features
    if temporal_analysis.get('peak_hour'):
        recommendations.append(f"⏰ Peak time alerts - Reports peak at {temporal_analysis['peak_hour']:02d}:00")
    
    # Geographic features
    if geo_analysis.get('spread') == 'concentrated':
        recommendations.append("🎯 Hotspot mapping - Geographic clustering detected")
    
    # Priority features
    if notes_analysis['priority_indicators'] > 0:
        recommendations.append("🚨 Priority escalation - Urgent content detected in notes")
    
    # Media features
    recommendations.append("📸 Photo verification - Visual evidence important for graffiti")
    
    # Analytics features
    recommendations.append("📊 Pattern analysis dashboard - For city planning")
    
    # Citizen engagement
    recommendations.append("🤝 Community cleanup coordination - Leverage citizen involvement")
    
    # Predictive features
    if temporal_analysis.get('weekday_distribution'):
        recommendations.append("🔮 Predictive analytics - Forecast based on day/time patterns")
    
    return recommendations

if __name__ == "__main__":
    analyze_graffiti_patterns()
