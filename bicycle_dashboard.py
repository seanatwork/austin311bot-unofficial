import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
import re

class BicycleDashboard:
    def __init__(self, db_path="bicycle_complaints.db"):
        self.db_path = db_path
        
    def load_data(self):
        """Load data from SQLite database"""
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql('SELECT * FROM bicycle_complaints', conn)
        conn.close()
        
        # Convert datetime columns
        df['created_date'] = pd.to_datetime(df['created_date'])
        df['scraped_at'] = pd.to_datetime(df['scraped_at'])
        
        return df
    
    def filter_by_date_range(self, df, days):
        """Filter data by date range"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        mask = (df['created_date'] >= start_date) & (df['created_date'] <= end_date)
        return df[mask]
    
    def create_overview_metrics(self, df):
        """Create overview metrics"""
        total_complaints = len(df)
        open_complaints = len(df[df['status'] == 'OPEN'])
        closed_complaints = len(df[df['status'] == 'CLOSED'])
        unique_addresses = df['address'].nunique()
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Total Complaints", total_complaints)
        
        with col2:
            st.metric("Open", open_complaints)
        
        with col3:
            st.metric("Closed", closed_complaints)
        
        with col4:
            st.metric("Unique Locations", unique_addresses)
    
    def create_timeline_chart(self, df):
        """Create timeline chart"""
        # Group by date
        daily_counts = df.groupby(df['created_date'].dt.date).size().reset_index()
        daily_counts.columns = ['date', 'count']
        
        fig = px.line(daily_counts, x='date', y='count', 
                     title='Complaints Over Time',
                     labels={'date': 'Date', 'count': 'Number of Complaints'})
        
        st.plotly_chart(fig, use_container_width=True)
    
    def get_top_complaints(self, df, limit=10):
        """Get top complaint descriptions"""
        # Clean up descriptions - remove template responses
        df_clean = df[df['description'].str.contains('COMPLETE|Evaluated|Referred|Resident Contacted') == False]
        
        top_complaints = df_clean['description'].value_counts().head(limit)
        
        st.subheader(f"Top {limit} Complaint Types")
        for i, (complaint, count) in enumerate(top_complaints.items(), 1):
            # Truncate long descriptions
            display_text = complaint[:100] + "..." if len(complaint) > 100 else complaint
            st.write(f"{i}. **{count}**: {display_text}")
    
    def get_top_locations(self, df, limit=10):
        """Get top complaint locations"""
        # Filter out default/empty addresses
        df_filtered = df[df['address'].str.len() > 5]
        
        top_locations = df_filtered['address'].value_counts().head(limit)
        
        st.subheader(f"Top {limit} Problem Locations")
        for i, (location, count) in enumerate(top_locations.items(), 1):
            st.write(f"{i}. **{count}**: {location}")
    
    def get_random_complaint(self, df):
        """Get a random complaint from filtered data"""
        if len(df) == 0:
            st.warning("No complaints found in the selected date range")
            return
        
        random_complaint = df.sample(1).iloc[0]
        
        st.subheader("🎲 Random Complaint")
        with st.expander(f"{random_complaint['ticket_number']} - {random_complaint['status']} - {random_complaint['created_date'].strftime('%Y-%m-%d')}", expanded=True):
            st.write(f"**Address:** {random_complaint['address']}")
            st.write(f"**Description:** {random_complaint['description']}")
            if random_complaint['response'] and len(random_complaint['response'].strip()) > 0:
                st.write(f"**Response:** {random_complaint['response']}")
            else:
                st.write("**Response:** No response recorded")
            
            # Add link to Austin 311 site
            ticket_url = f"https://311.austintexas.gov/tickets/{random_complaint['ticket_number'].replace('#', '')}"
            st.markdown(f"🔗 [View on Austin 311 Website]({ticket_url})")
    
    def create_location_map(self, df):
        """Create a simple location visualization"""
        # Filter out default addresses
        df_filtered = df[df['address'].str.len() > 5]
        
        location_counts = df_filtered['address'].value_counts().head(20)
        
        fig = px.bar(x=location_counts.values, y=location_counts.index,
                     orientation='h',
                     title='Top 20 Locations by Complaint Count')
        
        fig.update_layout(yaxis={'categoryorder': 'total ascending'})
        st.plotly_chart(fig, use_container_width=True)
    
    def show_recent_complaints(self, df, limit=10):
        """Show recent complaints"""
        st.subheader(f"Recent {limit} Complaints")
        
        recent_df = df.sort_values('created_date', ascending=False).head(limit)
        
        for _, row in recent_df.iterrows():
            with st.expander(f"{row['ticket_number']} - {row['status']} - {row['created_date'].strftime('%Y-%m-%d')}"):
                st.write(f"**Address:** {row['address']}")
                st.write(f"**Description:** {row['description']}")
                if row['response'] and len(row['response'].strip()) > 0:
                    st.write(f"**Response:** {row['response']}")
                
                # Add link to Austin 311 site
                ticket_url = f"https://311.austintexas.gov/tickets/{row['ticket_number'].replace('#', '')}"
                st.markdown(f"🔗 [View on Austin 311 Website]({ticket_url})")
    
    def run_dashboard(self):
        """Run the main dashboard"""
        st.set_page_config(page_title="Austin Bicycle Complaints Dashboard", layout="wide")
        
        st.title("🚴 Austin Bicycle Infrastructure Complaints")
        st.markdown("Dashboard for analyzing 311 bicycle-related complaints in Austin")
        
        # Load data
        with st.spinner("Loading data..."):
            df = self.load_data()
        
        # Sidebar for date filtering
        st.sidebar.header("Filters")
        
        # Date range options
        date_options = {
            "Last 30 days": 30,
            "Last 60 days": 60,
            "Last 90 days": 90,
            "Last 6 months": 180,
            "Last year": 365,
            "All data": None
        }
        
        selected_period = st.sidebar.selectbox("Select time period:", list(date_options.keys()))
        
        # Filter data based on selection
        if date_options[selected_period]:
            filtered_df = self.filter_by_date_range(df, date_options[selected_period])
            st.info(f"Showing data from the last {date_options[selected_period]} days ({len(filtered_df)} complaints)")
        else:
            filtered_df = df
            st.info(f"Showing all data ({len(filtered_df)} complaints)")
        
        # Overview metrics
        st.header("Overview")
        self.create_overview_metrics(filtered_df)
        
        # Charts
        st.header("Analytics")
        
        # Timeline chart (full width)
        self.create_timeline_chart(filtered_df)
        
        # Location analysis
        st.header("Location Analysis")
        self.create_location_map(filtered_df)
        
        # Top complaints and random complaint
        col1, col2 = st.columns(2)
        
        # Only show top complaints and locations if period is longer than 90 days
        show_detailed_analysis = date_options[selected_period] is None or date_options[selected_period] > 90
        
        if show_detailed_analysis:
            with col1:
                self.get_top_complaints(filtered_df, 10)
            
            with col2:
                self.get_top_locations(filtered_df, 10)
                st.subheader("🎲 Random Complaint Explorer")
                if st.button("Get Random Complaint", key="random_btn"):
                    self.get_random_complaint(filtered_df)
                else:
                    st.info("Click the button to see a random complaint from the selected time period")
        else:
            with col1:
                st.subheader("🎲 Random Complaint Explorer")
                if st.button("Get Random Complaint", key="random_btn"):
                    self.get_random_complaint(filtered_df)
                else:
                    st.info("Click the button to see a random complaint from the selected time period")
            
            with col2:
                st.info("📊 Select a longer time period (6+ months or all data) to see top complaint types and locations")
        
        # Recent complaints
        st.header("Recent Activity")
        self.show_recent_complaints(filtered_df, 10)
        
        # Data summary
        st.header("Data Summary")
        st.write(f"**Total database size:** {len(df)} complaints")
        st.write(f"**Date range:** {df['created_date'].min().strftime('%Y-%m-%d')} to {df['created_date'].max().strftime('%Y-%m-%d')}")
        st.write(f"**Last updated:** {df['scraped_at'].max().strftime('%Y-%m-%d %H:%M')}")

def main():
    dashboard = BicycleDashboard()
    dashboard.run_dashboard()

if __name__ == "__main__":
    main()
