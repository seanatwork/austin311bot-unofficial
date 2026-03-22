import pandas as pd
import streamlit as st
import plotly.express as px
from datetime import datetime

# Load the CSV
@st.cache_data  # Cache for performance
def load_data():
    df = pd.read_csv('311_Service_Requests_Last_365_Days.csv')
    # Convert 'Created Date' to datetime
    df['Created Date'] = pd.to_datetime(df['Created Date'], errors='coerce')
    # Drop rows with invalid dates if any
    df = df.dropna(subset=['Created Date'])
    return df

df = load_data()

# Title
st.title("311 Service Requests Dashboard")
st.markdown("A simple view of Austin Transportation and Public Works service requests.")

# Sidebar filters
st.sidebar.header("Filters")
# Date range filter
start_date = st.sidebar.date_input("Start Date", value=df['Created Date'].min().date())
end_date = st.sidebar.date_input("End Date", value=df['Created Date'].max().date())
# Department filter
departments = df['Department'].unique()
selected_dept = st.sidebar.multiselect("Select Department", departments, default=departments)

# Filter data
filtered_df = df[
    (df['Created Date'].dt.date >= start_date) &
    (df['Created Date'].dt.date <= end_date) &
    (df['Department'].isin(selected_dept))
]

# Section 1: Requests Over Time
st.header("Service Requests Over Time")
# Group by date and count
time_data = filtered_df.groupby(filtered_df['Created Date'].dt.date).size().reset_index(name='Count')
fig_time = px.line(time_data, x='Created Date', y='Count', title="Daily Request Volume")
st.plotly_chart(fig_time)

# Section 2: Most Common Service Descriptions
st.header("Most Common Service Request Descriptions")
desc_counts = filtered_df['Service Request Description'].value_counts().head(10).reset_index()
desc_counts.columns = ['Description', 'Count']
fig_desc = px.bar(desc_counts, x='Description', y='Count', title="Top 10 Descriptions")
st.plotly_chart(fig_desc)

# Section 3: Most Common Group Descriptions
st.header("Most Common Group Descriptions")
group_counts = filtered_df['Group Description'].value_counts().head(10).reset_index()
group_counts.columns = ['Group', 'Count']
fig_group = px.bar(group_counts, x='Group', y='Count', title="Top 10 Groups")
st.plotly_chart(fig_group)

# Section 4: Method Received Counts
st.header("Method Received Counts")
method_counts = filtered_df['Method Received'].value_counts().reset_index()
method_counts.columns = ['Method', 'Count']
fig_method = px.bar(method_counts, x='Method', y='Count', title="Requests by Method Received")
st.plotly_chart(fig_method)

# Optional: Raw data view
if st.checkbox("Show Raw Data"):
    st.dataframe(filtered_df.head(100))  # Limit for performance