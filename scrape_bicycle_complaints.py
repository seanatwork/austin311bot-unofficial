import requests
from bs4 import BeautifulSoup
import pandas as pd
import sqlite3
import time
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs

class BicycleComplaintsScraper:
    def __init__(self, db_path="bicycle_complaints.db", parquet_path="bicycle_complaints.parquet"):
        self.base_url = "https://311.austintexas.gov/tickets"
        self.db_path = db_path
        self.parquet_path = parquet_path
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
    def get_current_database_count(self):
        """Get current number of tickets in database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM bicycle_complaints')
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except:
            return 0
    
    def calculate_start_page(self, tickets_per_page=10):
        """Calculate which page to start scraping based on existing data"""
        current_count = self.get_current_database_count()
        if current_count == 0:
            return 1  # Start from beginning if no data
        else:
            # Calculate page number (add 1 to start on next page)
            start_page = (current_count // tickets_per_page) + 1
            print(f"Found {current_count} tickets in database, starting from page {start_page}")
            return start_page
        
    def setup_database(self):
        """Initialize SQLite database with proper schema"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bicycle_complaints (
                ticket_number TEXT PRIMARY KEY,
                address TEXT,
                description TEXT,
                response TEXT,
                status TEXT,
                created_date TEXT,
                scraped_at TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
        
    def scrape_page(self, page_num=1):
        """Scrape a single page of bicycle complaints"""
        params = {
            'filter[facets][ticket_type_code][]': 'PWBICYCL',
            'page': page_num
        }
        
        try:
            print(f"Scraping page {page_num}...")
            response = self.session.get(self.base_url, params=params, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            complaints = []
            
            # Find all complaint items based on the structure we observed
            complaint_items = soup.find_all('li', class_=lambda x: x and 'list-group-item' in x)
            
            if not complaint_items:
                # Try alternative selectors
                complaint_items = soup.find_all('div', class_=lambda x: x and 'ticket' in x.lower())
            
            if not complaint_items:
                # Look for any list items that contain ticket links
                complaint_items = soup.find_all('li')
                complaint_items = [item for item in complaint_items if item.find('a', href=lambda x: x and '/tickets/' in x)]
            
            for item in complaint_items:
                complaint = self.extract_complaint_data(item)
                if complaint:
                    complaints.append(complaint)
            
            print(f"  Found {len(complaints)} complaints on page {page_num}")
            return complaints
            
        except requests.exceptions.RequestException as e:
            print(f"Error scraping page {page_num}: {e}")
            return []
    
    def extract_complaint_data(self, item):
        """Extract complaint data from a list item"""
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
            
            # Extract address (usually the first text after the link)
            address = ""
            # Look for address patterns (street numbers + street names)
            address_text = item.get_text()
            address_match = re.search(r'(\d+\s+[^,\n]+(?:St|St|Ave|Ave|Rd|Rd|Blvd|Blvd|Dr|Dr|Ln|Ln)\s*,?\s*Austin)', address_text)
            if address_match:
                address = address_match.group(1)
            
            # Extract description (bold text) - citizen complaints, not city responses
            description = ""
            bold_elements = item.find_all(['b', 'strong'])
            for bold in bold_elements:
                bold_text = bold.get_text(strip=True)
                # Skip if it's just the ticket number, status, or city response template
                if (not bold_text.startswith('#') and 
                    bold_text.upper() not in ['OPEN', 'CLOSED'] and
                    not any(keyword in bold_text.lower() for keyword in [
                        'complete.', 'evaluated', 'addressed', 'referred', 'contacted', 
                        'close sr', 'csr', 'department', 'division', 'forwarded',
                        'thank you for', 'transportation', 'public works', 'atpw'
                    ])):
                    description = bold_text
                    break
            
            # If no bold text found, try to extract from the text structure (non-bold descriptions)
            if not description:
                # Look for text that appears to be a citizen complaint
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
                        not re.match(r'^\d+\s+', text)):  # Not an address
                        description = text
                        break
            
            # Extract status (OPEN/CLOSED)
            status = "UNKNOWN"
            status_elements = item.find_all(text=lambda x: x and x.strip().upper() in ['OPEN', 'CLOSED'])
            for status_text in status_elements:
                status = status_text.strip().upper()
                break
            
            # Extract response (usually appears after status for closed tickets)
            response = ""
            # Look for text that appears to be a response (often contains department names, actions taken)
            text_elements = item.get_text('\n').split('\n')
            for i, text in enumerate(text_elements):
                text = text.strip()
                # Look for response indicators
                if any(keyword in text.lower() for keyword in ['referred', 'evaluated', 'addressed', 'contacted', 'close sr', 'csr', 'department', 'division']):
                    response = text
                    break
            
            # Try to get created date from "ago" text and convert to actual datetime
            created_date = ""
            created_datetime = None
            ago_match = re.search(r'(\d+)([hdw])\s+ago', item.get_text())
            if ago_match:
                amount = int(ago_match.group(1))
                unit = ago_match.group(2)
                
                # Calculate actual datetime based on scrape time
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
                'address': address,
                'description': description,
                'response': response,
                'status': status,
                'created_date': created_date,
                'scraped_at': datetime.now().isoformat()
            }
            
        except Exception as e:
            print(f"Error extracting complaint data: {e}")
            return None
    
    def scrape_all_pages(self, start_page=1, max_pages=None, chunk_size=50, sleep_between_chunks=30):
        """Scrape pages in chunks with sleep timers to be respectful"""
        all_complaints = []
        page = start_page
        pages_in_current_chunk = 0
        
        print(f"Starting from page {start_page}, scraping in chunks of {chunk_size} pages")
        print(f"Sleeping {sleep_between_chunks} seconds between chunks to be respectful")
        
        while True:
            if max_pages and (page - start_page) >= max_pages:
                print(f"Reached maximum pages limit ({max_pages})")
                break
                
            complaints = self.scrape_page(page)
            
            if not complaints:
                print(f"No complaints found on page {page}, stopping...")
                break
                
            all_complaints.extend(complaints)
            pages_in_current_chunk += 1
            
            # Rate limiting - be respectful
            time.sleep(3)  # 3 seconds between pages
            
            # Check if we need to sleep between chunks
            if pages_in_current_chunk >= chunk_size:
                print(f"Completed chunk of {chunk_size} pages. Sleeping {sleep_between_chunks} seconds...")
                time.sleep(sleep_between_chunks)
                pages_in_current_chunk = 0
                
            page += 1
        
        return all_complaints
    
    def save_to_sqlite(self, complaints):
        """Save complaints to SQLite database"""
        if not complaints:
            print("No complaints to save!")
            return
            
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Use INSERT OR REPLACE to handle duplicates
        for complaint in complaints:
            cursor.execute('''
                INSERT OR REPLACE INTO bicycle_complaints 
                (ticket_number, address, description, response, status, created_date, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                complaint['ticket_number'],
                complaint['address'],
                complaint['description'],
                complaint['response'],
                complaint['status'],
                complaint['created_date'],
                complaint['scraped_at']
            ))
        
        conn.commit()
        conn.close()
        print(f"Saved {len(complaints)} complaints to SQLite database: {self.db_path}")
    
    def save_to_parquet(self, complaints):
        """Save complaints to Parquet file"""
        if not complaints:
            print("No complaints to save!")
            return
            
        df = pd.DataFrame(complaints)
        df.to_parquet(self.parquet_path, index=False)
        print(f"Saved {len(complaints)} complaints to Parquet file: {self.parquet_path}")
    
    def get_summary_stats(self, complaints):
        """Generate summary statistics"""
        if not complaints:
            return "No complaints found"
            
        df = pd.DataFrame(complaints)
        
        stats = {
            'total_complaints': len(df),
            'status_breakdown': df['status'].value_counts().to_dict(),
            'complaints_with_descriptions': len(df[df['description'].str.len() > 10]),
            'complaints_with_responses': len(df[df['response'].str.len() > 10]),
            'unique_addresses': df['address'].nunique()
        }
        
        return stats
    
    def run_full_scrape(self, max_pages=None, chunk_size=50, sleep_between_chunks=30):
        """Complete scraping and save process with smart continuation"""
        print("Starting bicycle complaints scraping...")
        
        # Setup database
        self.setup_database()
        
        # Calculate where to start
        start_page = self.calculate_start_page()
        
        if max_pages:
            print(f"Target: up to {max_pages} pages starting from page {start_page}")
        else:
            print(f"Target: scrape all pages starting from page {start_page}")
        
        # Scrape pages in chunks
        complaints = self.scrape_all_pages(start_page, max_pages, chunk_size, sleep_between_chunks)
        
        if not complaints:
            print("No new complaints scraped!")
            return None
        
        # Save to both formats
        self.save_to_sqlite(complaints)
        self.save_to_parquet(complaints)
        
        # Show summary
        total_count = self.get_current_database_count()
        print(f"\nDatabase now contains {total_count} total complaints")
        
        stats = self.get_summary_stats(complaints)
        print("\n=== This Session Statistics ===")
        import json
        print(json.dumps(stats, indent=2, default=str))
        
        return complaints

def main():
    # Initialize scraper
    scraper = BicycleComplaintsScraper()
    
    # Run the complete process with smart continuation
    # Parameters: max_pages=None (no limit), chunk_size=50, sleep_between_chunks=30
    complaints = scraper.run_full_scrape(max_pages=None, chunk_size=50, sleep_between_chunks=30)
    
    if complaints:
        print(f"\nSuccessfully scraped {len(complaints)} new bicycle complaints!")
        print(f"Data saved to: {scraper.db_path} and {scraper.parquet_path}")
        print("\nRun this script again to continue scraping more pages.")

if __name__ == "__main__":
    main()
