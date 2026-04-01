#!/usr/bin/env python3
"""
Explain how hotspot clustering works with actual graffiti data
"""

import sqlite3
from datetime import datetime
import math

def explain_hotspot_clustering():
    """Explain hotspot clustering with real data examples"""
    
    print("🗺️ HOTSPOT CLUSTERING EXPLANATION")
    print("=" * 50)
    
    # Get real graffiti data
    conn = sqlite3.connect("../311_categories.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT lat, long, address, service_request_id, requested_datetime
        FROM open311_requests
        WHERE service_code = 'HHSGRAFF' AND lat IS NOT NULL AND long IS NOT NULL
        ORDER BY requested_datetime DESC
        LIMIT 20
    """)
    
    records = cursor.fetchall()
    
    if len(records) < 2:
        print("Need at least 2 records to show clustering")
        return
    
    print(f"📊 Using {len(records)} recent graffiti records")
    print()
    
    # Show clustering threshold
    threshold = 0.001  # ~100 meters precision
    print(f"🔍 Clustering threshold: {threshold} degrees")
    print(f"   ≈ {threshold * 111000:.0f} meters precision")
    print(f"   Points within threshold cluster together")
    print()
    
    # Demonstrate clustering with first few records
    print("📍 CLUSTERING EXAMPLES:")
    
    clusters = {}
    
    for i, (lat, lon, address, sr_id, date) in enumerate(records[:10], 1):
        # Calculate cluster key
        cluster_lat = round(lat / threshold)
        cluster_lon = round(lon / threshold)
        cluster_key = (cluster_lat, cluster_lon)
        
        if cluster_key not in clusters:
            clusters[cluster_key] = []
        
        clusters[cluster_key].append((lat, lon, address, sr_id))
        
        # Calculate distances to other points in same cluster
        same_cluster_points = clusters[cluster_key]
        distances = []
        
        for other_lat, other_lon, other_addr, other_id in same_cluster_points:
            if other_id != sr_id:
                # Haversine distance formula
                distance = calculate_distance(lat, lon, other_lat, other_lon)
                distances.append(distance)
        
        print(f"\n{i}. 📍 Point: ({lat:.6f}, {lon:.6f})")
        print(f"   🏠 Address: {address}")
        print(f"   🎫 Cluster: ({cluster_lat}, {cluster_lon})")
        print(f"   📊 Cluster size: {len(same_cluster_points)} points")
        
        if distances:
            closest = min(distances)
            print(f"   📏 Closest neighbor: {closest:.0f} meters")
        else:
            print(f"   📏 Closest neighbor: Only point in cluster")
    
    print()
    print("🎯 HOTSPOT IDENTIFICATION:")
    
    # Show actual hotspots from our data
    for cluster_key, points in clusters.items():
        if len(points) >= 3:  # Hotspot threshold
            avg_lat = sum(p[0] for p in points) / len(points)
            avg_lon = sum(p[1] for p in points) / len(points)
            
            print(f"\n🔥 HOTSPOT FOUND:")
            print(f"   📍 Center: ({avg_lat:.6f}, {avg_lon:.6f})")
            print(f"   📊 Points: {len(points)}")
            print(f"   🏠 Addresses:")
            
            for lat, lon, addr, sr_id in points:
                print(f"      • {addr} (ID: {sr_id})")
    
    print()
    print("📏 DISTANCE CALCULATIONS:")
    print("Using Haversine formula for great-circle distance:")
    print("distance = 2 * R * arcsin(√(sin²(Δφ/2) + cos φ₁ * cos φ₂ * sin²(Δλ/2)))")
    print("where R = Earth's radius (6,371 km)")
    print()
    print("🔢 For our threshold of 0.001°:")
    print(f"   = {0.001 * 111000:.1f} meters")
    print(f"   = ~{0.001 * 111000/3.28:.1f} feet")
    
    conn.close()

def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two coordinates in meters"""
    R = 6371000  # Earth radius in meters
    
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    # Haversine formula
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    
    a = (math.sin(dlat/2)**2 + 
           math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    distance = R * c
    return distance

def show_proximity_analysis():
    """Show proximity analysis for graffiti records"""
    
    print("\n📏 PROXIMITY ANALYSIS FOR GRAFFITI:")
    print("=" * 50)
    
    conn = sqlite3.connect("../311_categories.db")
    cursor = conn.cursor()
    
    # Get records with coordinates
    cursor.execute("""
        SELECT lat, long, address, status, requested_datetime
        FROM open311_requests
        WHERE service_code = 'HHSGRAFF' AND lat IS NOT NULL AND long IS NOT NULL
        ORDER BY requested_datetime DESC
        LIMIT 50
    """)
    
    records = cursor.fetchall()
    
    print(f"📊 Analyzing {len(records)} records with coordinates")
    print()
    
    # Analyze proximity patterns
    close_proximity = []  # < 100m
    medium_proximity = []  # 100-500m  
    far_proximity = []  # > 500m
    
    for i, (lat1, lon1, addr1, status1, date1) in enumerate(records):
        for j, (lat2, lon2, addr2, status2, date2) in enumerate(records[i+1:], i+1):
            distance = calculate_distance(lat1, lon1, lat2, lon2)
            
            if distance < 100:
                close_proximity.append((addr1, addr2, distance, status1, status2))
            elif distance < 500:
                medium_proximity.append((addr1, addr2, distance, status1, status2))
            else:
                far_proximity.append((addr1, addr2, distance, status1, status2))
    
    print(f"🔴 CLOSE PROXIMITY (< 100m): {len(close_proximity)} pairs")
    for addr1, addr2, dist, status1, status2 in close_proximity[:5]:
        print(f"   {dist:.0f}m: {addr1} ↔ {addr2} ({status1} → {status2})")
    
    print(f"\n🟡 MEDIUM PROXIMITY (100-500m): {len(medium_proximity)} pairs")
    for addr1, addr2, dist, status1, status2 in medium_proximity[:3]:
        print(f"   {dist:.0f}m: {addr1} ↔ {addr2} ({status1} → {status2})")
    
    print(f"\n🔵 FAR PROXIMITY (> 500m): {len(far_proximity)} pairs")
    
    # Status proximity analysis
    close_open = sum(1 for _, _, _, status1, status2 in close_proximity 
                   if 'open' in [status1, status2])
    close_closed = sum(1 for _, _, _, status1, status2 in close_proximity 
                     if 'closed' in [status1, status2])
    
    print(f"\n📋 CLOSE PROXIMITY STATUS:")
    print(f"   Open cases: {close_open}")
    print(f"   Closed cases: {close_closed}")
    print(f"   Open rate: {close_open/(close_open+close_closed)*100:.1f}%")
    
    conn.close()

if __name__ == "__main__":
    explain_hotspot_clustering()
    show_proximity_analysis()
