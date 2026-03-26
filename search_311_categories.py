#!/usr/bin/env python3
"""
311 Category Search Tool

A flexible tool to scrape any 311 service category from Austin 311 website.
Based on scrape_bicycle_complaints.py but generalized for any category.
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import sqlite3
import time
import re
import typer
from typing import List, Optional
from typing_extensions import Annotated
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs, unquote
import json
import os

class Category311Scraper:
    def __init__(self, db_path="311_categories.db", parquet_path="311_categories.parquet"):
        self.base_url = "https://311.austintexas.gov/tickets"
        self.db_path = db_path
        self.parquet_path = parquet_path
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        self.categories = self.load_categories()
        
    def load_categories(self):
        """Load categories from the HTML file"""
        categories = {}
        html_file = "311austin.htm"
        
        if not os.path.exists(html_file):
            print(f"Warning: {html_file} not found. Using hardcoded categories.")
            return self.get_hardcoded_categories()
        
        try:
            with open(html_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            soup = BeautifulSoup(content, 'html.parser')
            
            # Find all links with filter patterns
            filter_links = soup.find_all('a', href=lambda x: x and 'filter%5Bfacets%5D%5Bticket_type_code%5D%5B%5D=' in x)
            
            for link in filter_links:
                href = link.get('href', '')
                aria_label = link.get('aria-label', '')
                
                # Extract category name from aria-label or link text
                if aria_label.startswith('Filter by '):
                    category_name = aria_label.replace('Filter by ', '')
                else:
                    category_name = link.get_text(strip=True)
                
                # Extract category code from href
                code_match = re.search(r'filter%5Bfacets%5D%5Bticket_type_code%5D%5B%5D=([^&]+)', href)
                if code_match:
                    category_code = unquote(code_match.group(1))
                    categories[category_name] = category_code
            
            print(f"Loaded {len(categories)} categories from {html_file}")
            return categories
            
        except Exception as e:
            print(f"Error loading categories from {html_file}: {e}")
            return self.get_hardcoded_categories()
    
    def get_hardcoded_categories(self):
        """Fallback hardcoded categories"""
        return {
            "Bicycle Issues": "PWBICYCL",
            "Parking Violation Enforcement": "PARKINGV",
            "Pothole Repair": "SBPOTREP",
            "Loose Dog": "ACLONAG",
            "Graffiti Abatement - Public Property": "HHSGRAFF",
            "Non Emergency Noise Complaint": "APDNONNO",
            "Traffic Signal - Maintenance": "TRASIGMA",
            "Animal Protection - Assistance Request": "ACINFORM",
            "Injured / Sick Animal": "COAACINJ",
            "Austin Energy Street Light Issue - Address": "STREETL2",
            "Adopted/Impounded/Surrendered Animal": "ASASISST",
            "Storm Debris Collection": "SWSSTORM",
            "Water Conservation Violation": "WWREPORT"
        }
    
    def list_categories(self):
        """Print all available categories"""
        print("\nAvailable 311 Categories:")
        print("=" * 50)
        for name, code in sorted(self.categories.items()):
            print(f"{name:<40} ({code})")
        print(f"\nTotal: {len(self.categories)} categories")
    
    def find_category(self, search_term):
        """Find categories matching search term"""
        matches = []
        search_lower = search_term.lower()
        
        for name, code in self.categories.items():
            if search_lower in name.lower():
                matches.append((name, code))
        
        return matches
    
    def setup_database(self):
        """Initialize SQLite database with proper schema"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS service_requests (
                ticket_number TEXT PRIMARY KEY,
                category_name TEXT,
                category_code TEXT,
                address TEXT,
                description TEXT,
                response TEXT,
                status TEXT,
                created_date TEXT,
                scraped_at TEXT
            )
        ''')
        
        # Create index for faster queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_category_code ON service_requests(category_code)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_created_date ON service_requests(created_date)
        ''')
        
        conn.commit()
        conn.close()
    
    def get_current_database_count(self, category_code=None):
        """Get current number of tickets in database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            if category_code:
                cursor.execute('SELECT COUNT(*) FROM service_requests WHERE category_code = ?', (category_code,))
            else:
                cursor.execute('SELECT COUNT(*) FROM service_requests')
            
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except:
            return 0
    
    def calculate_start_page(self, category_code, tickets_per_page=10):
        """Calculate which page to start scraping based on existing data"""
        current_count = self.get_current_database_count(category_code)
        if current_count == 0:
            return 1
        else:
            start_page = (current_count // tickets_per_page) + 1
            print(f"Found {current_count} tickets for {category_code} in database, starting from page {start_page}")
            return start_page
    
    def scrape_page(self, category_code, category_name, page_num=1):
        """Scrape a single page of service requests"""
        params = {
            'filter[facets][ticket_type_code][]': category_code,
            'page': page_num
        }
        
        try:
            print(f"Scraping page {page_num} for {category_name}...")
            response = self.session.get(self.base_url, params=params, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            requests_data = []
            
            # Find all request items based on the structure
            request_items = soup.find_all('li', class_=lambda x: x and 'list-group-item' in x)
            
            if not request_items:
                request_items = soup.find_all('div', class_=lambda x: x and 'ticket' in x.lower())
            
            if not request_items:
                request_items = soup.find_all('li')
                request_items = [item for item in request_items if item.find('a', href=lambda x: x and '/tickets/' in x)]
            
            for item in request_items:
                request_data = self.extract_request_data(item, category_name, category_code)
                if request_data:
                    requests_data.append(request_data)
            
            print(f"  Found {len(requests_data)} requests on page {page_num}")
            return requests_data
            
        except requests.exceptions.RequestException as e:
            print(f"Error scraping page {page_num}: {e}")
            return []
    
    def extract_request_data(self, item, category_name, category_code):
        """Extract request data from a list item"""
        try:
            # Find the ticket link and number
            ticket_link = item.find('a', href=lambda x: x and '/tickets/' in x)
            if not ticket_link:
                return None
                
            ticket_url = ticket_link.get('href', '')
            ticket_number = ticket_link.get_text(strip=True)
            
            # Extract ticket number from URL if not in text
            if not ticket_number or not ticket_number.startswith('#'):
                ticket_match = re.search(r'/tickets/([^/]+)', ticket_url)
                if ticket_match:
                    ticket_number = f"#{ticket_match.group(1)}"
            
            # Extract address
            address = ""
            address_text = item.get_text()
            address_match = re.search(r'(\d+\s+[^,\n]+(?:St|Ave|Rd|Blvd|Dr|Ln)\s*,?\s*Austin)', address_text)
            if address_match:
                address = address_match.group(1)
            
            # Extract description
            description = ""
            bold_elements = item.find_all(['b', 'strong'])
            for bold in bold_elements:
                bold_text = bold.get_text(strip=True)
                if (not bold_text.startswith('#') and 
                    bold_text.upper() not in ['OPEN', 'CLOSED'] and
                    not any(keyword in bold_text.lower() for keyword in [
                        'complete.', 'evaluated', 'addressed', 'referred', 'contacted', 
                        'close sr', 'csr', 'department', 'division', 'forwarded',
                        'thank you for', 'transportation', 'public works', 'atpw'
                    ]) and
                    not any(pattern in bold_text for pattern in [
                        'svg PUBLIC', 'DTD SVG', '//W3C//DTD', 'http://www.w3.org',
                        '<?xml', '<!DOCTYPE', '<svg', '</svg>'
                    ])):
                    description = bold_text
                    break
            
            if not description:
                text_elements = item.find_all(text=True)
                for i, text in enumerate(text_elements):
                    text = text.strip()
                    if (text and len(text) > 20 and 
                        not text.startswith('#') and 
                        text.upper() not in ['OPEN', 'CLOSED', 'AUSTIN'] and
                        not any(keyword in text.lower() for keyword in [
                            'complete.', 'evaluated', 'addressed', 'referred', 'contacted', 
                            'close sr', 'csr', 'department', 'division', 'forwarded',
                            'thank you for', 'transportation', 'public works', 'atpw'
                        ]) and
                        not re.match(r'^\d+\s+', text) and
                        not any(pattern in text for pattern in [
                            'svg PUBLIC', 'DTD SVG', '//W3C//DTD', 'http://www.w3.org',
                            '<?xml', '<!DOCTYPE', '<svg', '</svg>'
                        ])):
                        description = text
                        break
            
            # Extract status
            status = "UNKNOWN"
            status_elements = item.find_all(text=lambda x: x and x.strip().upper() in ['OPEN', 'CLOSED'])
            for status_text in status_elements:
                status = status_text.strip().upper()
                break
            
            # Extract response
            response = ""
            text_elements = item.get_text('\n').split('\n')
            for i, text in enumerate(text_elements):
                text = text.strip()
                if any(keyword in text.lower() for keyword in ['referred', 'evaluated', 'addressed', 'contacted', 'close sr', 'csr', 'department', 'division']):
                    response = text
                    break
            
            # Extract created date
            created_date = ""
            created_datetime = None
            ago_match = re.search(r'(\d+)([hdw])\s+ago', item.get_text())
            if ago_match:
                amount = int(ago_match.group(1))
                unit = ago_match.group(2)
                
                now = datetime.now()
                if unit == 'h':
                    created_datetime = now - timedelta(hours=amount)
                elif unit == 'd':
                    created_datetime = now - timedelta(days=amount)
                elif unit == 'w':
                    created_datetime = now - timedelta(weeks=amount)
                
                created_date = created_datetime.isoformat()
            
            return {
                'ticket_number': ticket_number,
                'category_name': category_name,
                'category_code': category_code,
                'address': address,
                'description': description,
                'response': response,
                'status': status,
                'created_date': created_date,
                'scraped_at': datetime.now().isoformat()
            }
            
        except Exception as e:
            print(f"Error extracting request data: {e}")
            return None
    
    def scrape_all_pages(self, category_code, category_name, start_page=1, max_pages=None, chunk_size=50, sleep_between_chunks=30):
        """Scrape pages in chunks with sleep timers"""
        all_requests = []
        page = start_page
        pages_in_current_chunk = 0
        
        print(f"Starting from page {start_page}, scraping in chunks of {chunk_size} pages")
        print(f"Sleeping {sleep_between_chunks} seconds between chunks to be respectful")
        
        while True:
            if max_pages and (page - start_page) >= max_pages:
                print(f"Reached maximum pages limit ({max_pages})")
                break
                
            requests = self.scrape_page(category_code, category_name, page)
            
            if not requests:
                print(f"No requests found on page {page}, stopping...")
                break
                
            all_requests.extend(requests)
            pages_in_current_chunk += 1
            
            # Rate limiting
            time.sleep(3)
            
            # Check if we need to sleep between chunks
            if pages_in_current_chunk >= chunk_size:
                print(f"Completed chunk of {chunk_size} pages. Sleeping {sleep_between_chunks} seconds...")
                time.sleep(sleep_between_chunks)
                pages_in_current_chunk = 0
                
            page += 1
        
        return all_requests
    
    def save_to_sqlite(self, requests):
        """Save requests to SQLite database"""
        if not requests:
            print("No requests to save!")
            return
            
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for request in requests:
            cursor.execute('''
                INSERT OR REPLACE INTO service_requests 
                (ticket_number, category_name, category_code, address, description, response, status, created_date, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                request['ticket_number'],
                request['category_name'],
                request['category_code'],
                request['address'],
                request['description'],
                request['response'],
                request['status'],
                request['created_date'],
                request['scraped_at']
            ))
        
        conn.commit()
        conn.close()
        print(f"Saved {len(requests)} requests to SQLite database: {self.db_path}")
    
    def save_to_parquet(self, requests):
        """Save requests to Parquet file"""
        if not requests:
            print("No requests to save!")
            return
            
        df = pd.DataFrame(requests)
        df.to_parquet(self.parquet_path, index=False)
        print(f"Saved {len(requests)} requests to Parquet file: {self.parquet_path}")
    
    def get_summary_stats(self, requests):
        """Generate summary statistics"""
        if not requests:
            return "No requests found"
            
        df = pd.DataFrame(requests)
        
        stats = {
            'total_requests': len(df),
            'category': df['category_name'].iloc[0] if not df.empty else 'Unknown',
            'status_breakdown': df['status'].value_counts().to_dict(),
            'requests_with_descriptions': len(df[df['description'].str.len() > 10]),
            'requests_with_responses': len(df[df['response'].str.len() > 10]),
            'unique_addresses': df['address'].nunique()
        }
        
        return stats
    
    def scrape_category(self, category_name, category_code, max_pages=None, chunk_size=50, sleep_between_chunks=30):
        """Complete scraping process for a specific category"""
        print(f"Starting scraping for category: {category_name} ({category_code})")
        
        # Setup database
        self.setup_database()
        
        # Calculate where to start
        start_page = self.calculate_start_page(category_code)
        
        if max_pages:
            print(f"Target: up to {max_pages} pages starting from page {start_page}")
        else:
            print(f"Target: scrape all pages starting from page {start_page}")
        
        # Scrape pages
        requests = self.scrape_all_pages(category_code, category_name, start_page, max_pages, chunk_size, sleep_between_chunks)
        
        if not requests:
            print("No new requests scraped!")
            return None
        
        # Save to both formats
        self.save_to_sqlite(requests)
        self.save_to_parquet(requests)
        
        # Show summary
        total_count = self.get_current_database_count(category_code)
        print(f"\nDatabase now contains {total_count} total requests for {category_name}")
        
        stats = self.get_summary_stats(requests)
        print("\n=== This Session Statistics ===")
        print(json.dumps(stats, indent=2, default=str))
        
        return requests

def complete_category(incomplete: str):
    """Provides tab completion for category names."""
    scraper = Category311Scraper()
    return [name for name in scraper.categories.keys() if incomplete.lower() in name.lower()]

def run_scrape(
    categories: Annotated[
        Optional[List[str]], 
        typer.Argument(help='Category name(s) to search for (e.g., "Bicycle")', autocompletion=complete_category)
    ] = None,
    list_all: Annotated[bool, typer.Option("--list", help="List all available categories")] = False,
    max_pages: Annotated[Optional[int], typer.Option(help="Maximum number of pages to scrape")] = None,
    chunk_size: Annotated[int, typer.Option(help="Pages per chunk before sleeping")] = 50,
    sleep_seconds: Annotated[int, typer.Option("--sleep", help="Seconds to sleep between chunks")] = 30,
):
    """Scrape Austin 311 service requests by category with native-feeling CLI."""
    scraper = Category311Scraper()
    
    if list_all:
        scraper.list_categories()
        return
    
    if not categories:
        typer.echo("No categories provided. Use --list to see available categories or --help for usage.")
        raise typer.Exit()

    all_matches = []
    for search_term in categories:
        matches = scraper.find_category(search_term)
        if not matches:
            print(f"No categories found matching: {search_term}")
            continue
        all_matches.extend(matches)
    
    if not all_matches:
        print("No matching categories found. Use --list to see available categories.")
        return
    
    # Show matches and confirm
    print(f"\nFound {len(all_matches)} matching categories:")
    for name, code in all_matches:
        print(f"  - {name} ({code})")
    
    # Scrape each category
    for category_name, category_code in all_matches:
        print(f"\n{'='*60}")
        requests = scraper.scrape_category(
            category_name, 
            category_code, 
            max_pages=max_pages,
            chunk_size=chunk_size,
            sleep_between_chunks=sleep_seconds
        )
        
        if requests:
            print(f"\nSuccessfully scraped {len(requests)} new requests for {category_name}!")
        
        # Small break between categories
        if len(all_matches) > 1:
            print("Waiting 10 seconds before next category...")
            time.sleep(10)
    
    print(f"\nAll done! Data saved to: {scraper.db_path} and {scraper.parquet_path}")

def main():
    """Entry point for the search-311 command."""
    # typer.run provides the bridge between sys.argv and our refactored run_scrape function
    typer.run(run_scrape)

if __name__ == "__main__":
    main()
