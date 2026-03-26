import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px
from datetime import datetime, timedelta
from collections import Counter

class BicycleDashboard:
    INVALID_DESCRIPTION_PATTERN = r"svg PUBLIC|DOCTYPE|COMPLETE|Evaluated|Referred|Resident Contacted"
    TOPIC_CHART_LIMIT = 15
    TOPIC_DROPDOWN_LIMIT = 20
    DEFAULT_PAGE_SIZE = 10

    def __init__(self, db_path="311_categories.db"):
        self.db_path = db_path
        
    def load_data(self):
        """Load data from SQLite database"""
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql('SELECT * FROM service_requests', conn)
        conn.close()
        
        # Convert datetime columns
        df['created_date'] = pd.to_datetime(df['created_date'])
        df['scraped_at'] = pd.to_datetime(df['scraped_at'])
        
        return df

    def _is_displayable_description(self, description):
        return not pd.isna(description) and "svg PUBLIC" not in description and "DOCTYPE" not in description

    def _format_description(self, description):
        if self._is_displayable_description(description):
            return description
        return "[SVG/HTML content - not displayable]"

    def _format_response(self, response):
        if isinstance(response, str) and response.strip():
            return response
        return "No response recorded"

    def _ticket_url(self, ticket_number):
        return f"https://311.austintexas.gov/tickets/{str(ticket_number).replace('#', '')}"

    def _clean_descriptions(self, df):
        return df[
            (~df["description"].str.contains(self.INVALID_DESCRIPTION_PATTERN, na=False))
            & (df["description"].str.len() > 10)
            & (df["description"].notna())
        ].copy()

    def _classify_topic(self, description, category):
        desc = str(description).lower()
        category_name = str(category)

        if "pothole" in desc:
            if "bike lane" in desc:
                return "Pothole in bike lane"
            if "intersection" in desc:
                return "Pothole at intersection"
            if "shoulder" in desc:
                return "Pothole on road shoulder"
            return "General pothole"

        if category_name == "Bicycle Issues":
            if "lane" in desc:
                if "blocked" in desc or "obstruction" in desc:
                    return "Blocked bike lane"
                if "debris" in desc or "glass" in desc:
                    return "Debris in bike lane"
                if "paint" in desc or "marking" in desc:
                    return "Bike lane marking issue"
                return "Bike lane issue"
            if "rack" in desc:
                return "Bike rack issue"
            if "path" in desc or "trail" in desc:
                return "Bike path/trail issue"
            if "light" in desc:
                return "Bike lighting issue"
            return "General bicycle issue"

        if "drain" in desc or "drainage" in desc:
            return "Drainage issue"
        if "sidewalk" in desc:
            return "Sidewalk issue"
        if "street light" in desc or "lighting" in desc:
            return "Street lighting issue"
        if "sign" in desc:
            return "Traffic sign issue"
        if "signal" in desc:
            return "Traffic signal issue"
        if "debris" in desc:
            return "Road debris"
        if "flooding" in desc:
            return "Flooding issue"
        if "tree" in desc:
            return "Tree issue"
        if "graffiti" in desc:
            return "Graffiti"
        if "repair" in desc:
            return "Repair request"
        if "maintenance" in desc:
            return "Maintenance request"
        if "clean" in desc:
            return "Cleaning request"
        if "hazard" in desc:
            return "Safety hazard"
        if "dangerous" in desc:
            return "Dangerous condition"
        return "Other issue"

    def extract_topics(self, df):
        """Extract topics from descriptions"""
        return [
            self._classify_topic(row["description"], row["category_name"])
            for _, row in df.iterrows()
        ]
    
    def get_topic_analysis(self, df):
        """Get topic analysis with counts"""
        df_clean = self._clean_descriptions(df)
        
        # Extract topics
        topics = self.extract_topics(df_clean)
        df_clean['topic'] = topics
        
        # Count topics
        topic_counts = Counter(topics)
        
        return df_clean, topic_counts
    
    def show_topic_details(self, df, selected_topic):
        """Show detailed tickets for selected topic"""
        st.subheader(f"📋 Tickets for: {selected_topic}")
        
        # Filter data for selected topic
        df_clean, _ = self.get_topic_analysis(df)
        topic_df = df_clean[df_clean['topic'] == selected_topic].copy()
        
        if len(topic_df) == 0:
            st.warning(f"No tickets found for topic: {selected_topic}")
            return
        
        st.info(f"Found {len(topic_df)} tickets for '{selected_topic}'")
        
        # Sort options
        sort_col = st.selectbox("Sort by:", ['created_date', 'status', 'address'], key="topic_sort")
        ascending = st.checkbox("Sort ascending", value=False, key="topic_asc")
        
        topic_df_sorted = topic_df.sort_values(sort_col, ascending=ascending)
        
        # Pagination
        items_per_page = 10
        total_pages = (len(topic_df_sorted) + items_per_page - 1) // items_per_page
        
        if total_pages > 1:
            page = st.selectbox("Page:", range(1, total_pages + 1), key="topic_page")
            start_idx = (page - 1) * items_per_page
            end_idx = start_idx + items_per_page
            display_df = topic_df_sorted.iloc[start_idx:end_idx]
        else:
            display_df = topic_df_sorted
        
        # Display tickets
        for _, row in display_df.iterrows():
            with st.expander(f"{row['ticket_number']} - {row['status']} - {row['created_date'].strftime('%Y-%m-%d')}"):
                col1, col2 = st.columns(2)
                with col1:
                    st.write(f"**Address:** {row['address']}")
                    st.write(f"**Category:** {row['category_name']}")
                    st.write(f"**Status:** {row['status']}")
                with col2:
                    st.write(f"**Created:** {row['created_date'].strftime('%Y-%m-%d %H:%M')}")
                    st.write(f"**Topic:** {row['topic']}")
                
                st.write(f"**Description:** {self._format_description(row['description'])}")
                st.write(f"**Response:** {self._format_response(row['response'])}")
                st.markdown(f"🔗 [View on Austin 311 Website]({self._ticket_url(row['ticket_number'])})")
    
    def create_topic_chart(self, df):
        """Create interactive topic chart"""
        df_clean, topic_counts = self.get_topic_analysis(df)
        
        # Create dataframe for chart
        topic_df = pd.DataFrame([
            {'Topic': topic, 'Count': count}
            for topic, count in topic_counts.most_common(self.TOPIC_CHART_LIMIT)
        ])
        
        # Create bar chart with click data
        fig = px.bar(topic_df, x='Count', y='Topic', orientation='h',
                     title='Top 15 Topics by Count (Click to explore)',
                     hover_data=['Count'])
        
        fig.update_layout(yaxis={'categoryorder': 'total ascending'})
        
        # Add click event handling
        fig.update_traces(
            hovertemplate='<b>%{y}</b><br>Count: %{x}<extra></extra>',
            marker_color='lightblue'
        )
        
        return fig, topic_counts
    
    def filter_by_date_range(self, df, days):
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
        
        st.plotly_chart(fig, width='stretch')
    
    def get_top_complaints(self, df, limit=10):
        """Get top complaint descriptions"""
        df_clean = self._clean_descriptions(df)
        
        # Extract complaint patterns
        complaint_patterns = []
        for desc in df_clean['description']:
            desc_lower = desc.lower()
            
            # Common pothole-related patterns
            if 'pothole' in desc_lower:
                if 'bike lane' in desc_lower:
                    complaint_patterns.append('Pothole in bike lane')
                elif 'shoulder' in desc_lower:
                    complaint_patterns.append('Pothole on road shoulder')
                elif 'intersection' in desc_lower:
                    complaint_patterns.append('Pothole at intersection')
                else:
                    complaint_patterns.append('General pothole')
            elif 'bike lane' in desc_lower:
                if 'debris' in desc_lower or 'glass' in desc_lower:
                    complaint_patterns.append('Debris in bike lane')
                elif 'obstruction' in desc_lower or 'blocked' in desc_lower:
                    complaint_patterns.append('Blocked bike lane')
                else:
                    complaint_patterns.append('Bike lane issue')
            elif 'street' in desc_lower and ('repair' in desc_lower or 'maintenance' in desc_lower):
                complaint_patterns.append('Street repair needed')
            elif 'drainage' in desc_lower or 'flooding' in desc_lower:
                complaint_patterns.append('Drainage/flooding issue')
            elif 'traffic' in desc_lower and ('sign' in desc_lower or 'signal' in desc_lower):
                complaint_patterns.append('Traffic sign/signal issue')
            elif 'sidewalk' in desc_lower:
                complaint_patterns.append('Sidewalk issue')
            elif 'lighting' in desc_lower or 'street light' in desc_lower:
                complaint_patterns.append('Street lighting issue')
            else:
                # Extract first meaningful phrase as fallback
                words = desc.split()
                if len(words) >= 3:
                    complaint_patterns.append(' '.join(words[:3]) + '...')
                else:
                    complaint_patterns.append(desc[:50] + '...' if len(desc) > 50 else desc)
        
        # Count patterns
        pattern_counts = pd.Series(complaint_patterns).value_counts().head(limit)
        
        st.subheader(f"Top {limit} Complaint Types")
        for i, (complaint, count) in enumerate(pattern_counts.items(), 1):
            st.write(f"{i}. **{count}**: {complaint}")
    
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
            st.write(f"**Category:** {random_complaint['category_name']}")
            st.write(f"**Description:** {self._format_description(random_complaint['description'])}")
            st.write(f"**Response:** {self._format_response(random_complaint['response'])}")
            st.markdown(f"🔗 [View on Austin 311 Website]({self._ticket_url(random_complaint['ticket_number'])})")
    
    def create_location_map(self, df):
        """Create a simple location visualization"""
        # Filter out default addresses
        df_filtered = df[df['address'].str.len() > 5]
        
        location_counts = df_filtered['address'].value_counts().head(20)
        
        fig = px.bar(x=location_counts.values, y=location_counts.index,
                     orientation='h',
                     title='Top 20 Locations by Complaint Count')
        
        fig.update_layout(yaxis={'categoryorder': 'total ascending'})
        st.plotly_chart(fig, width='stretch')
    
    def show_recent_complaints(self, df, limit=10):
        """Show recent complaints"""
        st.subheader(f"Recent {limit} Complaints")
        
        recent_df = df.sort_values('created_date', ascending=False).head(limit)
        
        for _, row in recent_df.iterrows():
            with st.expander(f"{row['ticket_number']} - {row['status']} - {row['created_date'].strftime('%Y-%m-%d')}"):
                st.write(f"**Address:** {row['address']}")
                st.write(f"**Category:** {row['category_name']}")
                st.write(f"**Description:** {self._format_description(row['description'])}")
                st.write(f"**Response:** {self._format_response(row['response'])}")
                st.markdown(f"🔗 [View on Austin 311 Website]({self._ticket_url(row['ticket_number'])})")
    
    def run_dashboard(self):
        """Run the main dashboard"""
        st.set_page_config(page_title="Austin 311 Service Requests Dashboard", layout="wide")
        
        st.title("🚴 Austin 311 Service Requests Dashboard")
        st.markdown("Dashboard for analyzing 311 service requests in Austin")
        
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
        
        # Topic Analysis Section
        st.header("📊 Topic Analysis")
        
        # Create topic chart
        fig, topic_counts = self.create_topic_chart(filtered_df)
        
        # Use plotly_chart_event to capture clicks
        chart_event = st.plotly_chart(fig, width='stretch', key="topic_chart", on_select="rerun")
        
        # Handle chart click events
        selected_topic_from_chart = None
        if chart_event and chart_event.selection and chart_event.selection.points:
            # Get the clicked topic from the chart
            clicked_point = chart_event.selection.points[0]
            # Access the topic name from the click data
            selected_topic_from_chart = clicked_point.get('y') or clicked_point.get('label')
        
        # Topic selection for detailed view
        st.subheader("🔍 Explore Topics")
        
        # Priority: chart click > dropdown selection
        topic_options = [topic for topic, _ in topic_counts.most_common(self.TOPIC_DROPDOWN_LIMIT)]
        
        # Create columns for better layout
        col1, col2 = st.columns([1, 2])
        
        with col1:
            st.write("**Or select manually:**")
            selected_topic_dropdown = st.selectbox("Select a topic:", [""] + topic_options, key="topic_dropdown")
        
        with col2:
            if selected_topic_from_chart:
                st.success(f"🎯 Clicked on: **{selected_topic_from_chart}**")
                selected_topic = selected_topic_from_chart
            elif selected_topic_dropdown:
                selected_topic = selected_topic_dropdown
            else:
                selected_topic = None
                st.info("Click on a bar chart above or select a topic from dropdown")
        
        # Show ticket details if a topic is selected
        if selected_topic:
            st.divider()
            self.show_topic_details(filtered_df, selected_topic)
        
        # Charts
        st.header("Analytics")
        
        # Timeline chart (full width)
        self.create_timeline_chart(filtered_df)
        
        # Location analysis
        st.header("Location Analysis")
        self.create_location_map(filtered_df)
        
        # Top complaints, locations, and random complaint
        col1, col2 = st.columns(2)
        
        # Only show top complaints and locations if period is longer than 90 days
        show_detailed_analysis = date_options[selected_period] is None or date_options[selected_period] > 90
        
        with col1:
            if show_detailed_analysis:
                self.get_top_complaints(filtered_df, 10)
            else:
                st.info("📊 Select a longer time period (6+ months or all data) to see top complaint types")

        with col2:
            if show_detailed_analysis:
                self.get_top_locations(filtered_df, 10)
            else:
                st.info("📍 Select a longer time period (6+ months or all data) to see top locations")

            st.subheader("🎲 Random Complaint Explorer")
            if st.button("Get Random Complaint", key="random_btn"):
                self.get_random_complaint(filtered_df)
            else:
                st.info("Click the button to see a random complaint from the selected time period")
        
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
