import pandas as pd
import sqlite3
from datetime import datetime, timedelta
import json

class BicycleComplaintsAnalyzer:
    def __init__(self, db_path="bicycle_complaints.db"):
        self.db_path = db_path
        
    def load_data(self):
        """Load data from SQLite database"""
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql('SELECT * FROM bicycle_complaints', conn)
        conn.close()
        
        # Convert datetime columns
        if 'scraped_at' in df.columns:
            df['scraped_at'] = pd.to_datetime(df['scraped_at'])
        
        return df
    
    def get_common_complaints(self, df, limit=10):
        """Get most common complaint descriptions"""
        # Group by description and count
        common_complaints = df['description'].value_counts().head(limit)
        return common_complaints
    
    def get_status_breakdown(self, df):
        """Get breakdown by complaint status"""
        return df['status'].value_counts()
    
    def get_location_hotspots(self, df, limit=10):
        """Get top locations by complaint count"""
        # Group by address and count
        location_counts = df['address'].value_counts().head(limit)
        return location_counts
    
    def get_response_analysis(self, df):
        """Analyze response patterns"""
        # Filter for closed complaints with responses
        closed_with_response = df[(df['status'] == 'CLOSED') & (df['response'].str.len() > 10)]
        
        # Common response patterns
        response_patterns = {}
        if len(closed_with_response) > 0:
            # Look for common response keywords
            responses = closed_with_response['response'].str.lower()
            
            keywords = ['referred', 'evaluated', 'addressed', 'contacted', 'close sr', 'department', 'division']
            for keyword in keywords:
                count = responses.str.contains(keyword, na=False).sum()
                if count > 0:
                    response_patterns[keyword] = count
        
        return response_patterns
    
    def generate_report(self, df):
        """Generate comprehensive analysis report"""
        print("=== Bicycle Infrastructure Complaints Analysis ===\n")
        
        # Basic stats
        print(f"Total Complaints: {len(df)}")
        if 'scraped_at' in df.columns:
            print(f"Last Scraped: {df['scraped_at'].max().strftime('%Y-%m-%d %H:%M')}")
        
        print(f"Complaints with Descriptions: {len(df[df['description'].str.len() > 10])}")
        print(f"Complaints with Responses: {len(df[df['response'].str.len() > 10])}")
        print(f"Unique Addresses: {df['address'].nunique()}\n")
        
        # Status breakdown
        print("=== Status Breakdown ===")
        status = self.get_status_breakdown(df)
        for status_type, count in status.items():
            print(f"{status_type}: {count}")
        
        # Most common complaints
        print("\n=== Top 10 Most Common Complaints ===")
        common = self.get_common_complaints(df)
        for i, (complaint, count) in enumerate(common.items(), 1):
            if pd.notna(complaint) and len(str(complaint)) > 0:
                print(f"{i}. {count}: {str(complaint)[:100]}...")
        
        # Location hotspots
        print("\n=== Top 10 Location Hotspots ===")
        locations = self.get_location_hotspots(df)
        for i, (location, count) in enumerate(locations.items(), 1):
            if pd.notna(location) and len(str(location)) > 0:
                print(f"{i}. {count}: {location}")
        
        # Response analysis
        print("\n=== Response Pattern Analysis ===")
        patterns = self.get_response_analysis(df)
        if patterns:
            for pattern, count in patterns.items():
                print(f"{pattern}: {count}")
        else:
            print("No response patterns found")
        
        # Recent complaints
        print("\n=== Recent Complaints (Last 5) ===")
        if 'scraped_at' in df.columns:
            recent = df.sort_values('scraped_at', ascending=False).head(5)
            for _, row in recent.iterrows():
                print(f"{row['ticket_number']} - {row['status']} - {row['address']}")
                if pd.notna(row['description']) and len(str(row['description'])) > 0:
                    print(f"  Issue: {str(row['description'])[:80]}...")
                print()

def main():
    analyzer = BicycleComplaintsAnalyzer()
    
    try:
        df = analyzer.load_data()
        if df.empty:
            print("No data found! Run scrape_bicycle_complaints.py first.")
            return
            
        analyzer.generate_report(df)
    except Exception as e:
        print(f"Error loading data: {e}")
        print("Make sure to run scrape_bicycle_complaints.py first!")

if __name__ == "__main__":
    main()
