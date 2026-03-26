#!/usr/bin/env python3
"""
Test script for the 311 Category Search Tool
"""

from search_311_categories import Category311Scraper

def test_category_loading():
    """Test that categories load correctly"""
    scraper = Category311Scraper()
    
    print("=== Testing Category Loading ===")
    print(f"Loaded {len(scraper.categories)} categories")
    
    # Test some known categories
    test_categories = ["Bicycle Issues", "Parking Violation Enforcement", "Pothole Repair"]
    
    for category in test_categories:
        matches = scraper.find_category(category)
        if matches:
            print(f"✓ Found '{category}': {matches[0][1]}")
        else:
            print(f"✗ Missing '{category}'")
    
    # Test search functionality
    print("\n=== Testing Search Functionality ===")
    search_terms = ["parking", "bicycle", "pothole"]
    
    for term in search_terms:
        matches = scraper.find_category(term)
        print(f"Search '{term}': found {len(matches)} matches")
        for name, code in matches[:3]:  # Show first 3
            print(f"  - {name} ({code})")

if __name__ == "__main__":
    test_category_loading()
