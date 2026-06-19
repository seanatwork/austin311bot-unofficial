#!/usr/bin/env python3
"""
Discover service codes for homeless-related categories.

Queries Open311 API to find service codes for:
- "Homeless - Violet Kiosk and Storage Carts"
- "Homelessness Matters"
"""

import os
import sys
import requests
import json
from datetime import datetime, timezone, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API_KEY = os.getenv("AUSTINAPIKEY", "")
BASE_URL = "https://311.austintexas.gov/open311/v2"


def get_service_list():
    """Get list of all available services from Open311."""
    url = f"{BASE_URL}/services.json"
    params = {}
    if API_KEY:
        params["api_key"] = API_KEY
    
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching services: {e}")
        return []


def search_services_by_keyword(services, keywords):
    """Search services by keywords."""
    matches = []
    for service in services:
        service_name = service.get("service_name", "").lower()
        description = service.get("description", "").lower()
        
        for keyword in keywords:
            if keyword.lower() in service_name or keyword.lower() in description:
                matches.append(service)
                break
    return matches


def test_service_code(code, days=7):
    """Test a service code by fetching recent requests."""
    url = f"{BASE_URL}/requests.json"
    
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days)
    
    params = {
        "service_code": code,
        "start_date": start_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date": end_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_page": 5,
    }
    if API_KEY:
        params["api_key"] = API_KEY
    
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return len(data)
    except Exception as e:
        print(f"  Error testing {code}: {e}")
        return 0


def discover_by_searching_requests():
    """
    Alternative approach: Search recent requests for homeless keywords
    and extract service codes from matches.
    """
    print("\n" + "="*70)
    print("DISCOVERY METHOD 2: Searching recent requests")
    print("="*70)
    
    # Search for requests with homeless-related keywords in description
    keywords = ["homeless", "encampment", "tent", "violet kiosk", "storage cart"]
    
    url = f"{BASE_URL}/requests.json"
    
    # Try different service codes we know and search for homeless keywords
    known_codes = [
        "PRGRDISS", "ATCOCIRW", "OBSTMIDB", "SBDEBROW", "DRCHANEL",
        "HHSGRAFF", "APDNONNO", "PARKINGV", "SBPOTREP", "TRASIGMA"
    ]
    
    discovered = {}
    
    for code in known_codes:
        params = {
            "service_code": code,
            "per_page": 100,
        }
        if API_KEY:
            params["api_key"] = API_KEY
        
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            for record in data:
                desc = (record.get("description") or "").lower()
                status_notes = (record.get("status_notes") or "").lower()
                full_text = f"{desc} {status_notes}"
                
                for keyword in keywords:
                    if keyword in full_text:
                        service_code = record.get("service_code")
                        service_name = record.get("service_name", "Unknown")
                        
                        if service_code not in discovered:
                            discovered[service_code] = {
                                "name": service_name,
                                "matches": set()
                            }
                        discovered[service_code]["matches"].add(keyword)
                        break
            
            print(f"  Checked {code}: {len(data)} requests")
            
        except Exception as e:
            print(f"  Error checking {code}: {e}")
    
    print("\n" + "="*70)
    print("DISCOVERED SERVICE CODES WITH HOMELESS KEYWORDS")
    print("="*70)
    
    if discovered:
        for code, info in discovered.items():
            print(f"\n{code}:")
            print(f"  Name: {info['name']}")
            print(f"  Keywords found: {', '.join(info['matches'])}")
    else:
        print("No additional service codes discovered with homeless keywords.")
    
    return discovered


def main():
    print("="*70)
    print("DISCOVERING HOMELESS-RELATED SERVICE CODES")
    print("="*70)
    
    # Method 1: Get service list and search
    print("\n" + "="*70)
    print("DISCOVERY METHOD 1: Service list search")
    print("="*70)
    
    services = get_service_list()
    print(f"\nFetched {len(services)} services from Open311")
    
    # Search for homeless-related keywords
    homeless_keywords = ["homeless", "violet", "kiosk", "storage", "cart", "encampment"]
    matches = search_services_by_keyword(services, homeless_keywords)
    
    print(f"\nFound {len(matches)} services matching homeless keywords:")
    print("-" * 70)
    
    for service in matches:
        code = service.get("service_code")
        name = service.get("service_name")
        desc = service.get("description", "")
        
        print(f"\nService Code: {code}")
        print(f"Name: {name}")
        print(f"Description: {desc[:100]}..." if len(desc) > 100 else f"Description: {desc}")
        
        # Test the code
        count = test_service_code(code)
        print(f"Recent requests (7 days): {count}")
    
    if not matches:
        print("No services found with 'homeless', 'violet', 'kiosk', 'storage', 'cart', or 'encampment' in name/description")
    
    # Method 2: Search through recent requests
    discovered = discover_by_searching_requests()
    
    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    
    all_codes = set()
    for service in matches:
        all_codes.add(service.get("service_code"))
    for code in discovered:
        all_codes.add(code)
    
    print(f"\nTotal unique service codes found: {len(all_codes)}")
    if all_codes:
        print("\nAdd these to your SERVICE_CODES in homeless_bot.py:")
        for code in sorted(all_codes):
            print(f'    "{code}": "Homeless-related",')
    else:
        print("\nNo new homeless-specific service codes discovered.")
        print("The categories 'Homeless - Violet Kiosk and Storage Carts' and 'Homelessness Matters'")
        print("likely share service codes with other categories (like Park Maintenance).")
        print("\nYour current 5 codes should still capture these reports through keyword filtering.")


if __name__ == "__main__":
    main()
