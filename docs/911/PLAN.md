# Austin 911 Response Time Trends Dashboard

## Goal
Create a trends dashboard showing how 911 response times have fluctuated over time (across the entire dataset, however many years it covers).

## High-Level Questions to Answer

1. **How many years of data does the dataset cover?**
   - Need to find the earliest and latest `response_datetime` values

2. **How has the average response time changed year over year?**
   - Monthly? Quarterly? Yearly?

3. **Are response times getting better or worse?**
   - Trend line over time

4. **Do different priority levels have different trends?**
   - Priority 1 vs Priority 2 vs Priority 3

5. **Do different council districts have different trends?**
   - Which districts have the best/worst response times?

6. **How do mental health calls compare?**
   - Are mental health response times different?

## Data Needed

From the API, we need:
- `response_datetime` (when call was received)
- `first_unit_arrived_datetime` (when first unit arrived)
- `priority_level` (to filter by urgency)
- `council_district` (to filter by location)
- `mental_health_flag` (to filter by call type)
- `incident_type` (to filter by dispatch type)

## Response Time Calculation

Response time = `first_unit_arrived_datetime` - `response_datetime`

This gives us minutes (or seconds) between call and arrival.

## Dashboard Sections (Draft)

1. **Overview Stats**
   - Date range of dataset (earliest to latest)
   - Total dispatches
   - Average response time overall

2. **Response Time Over Time**
   - Line chart: average response time by month
   - Show trend line (increasing or decreasing)

3. **By Priority Level**
   - Separate lines for Priority 1, 2, 3
   - Do high-priority calls have different trends?

4. **By Council District**
   - Compare response times across districts
   - Which districts are fastest/slowest?

5. **Mental Health Calls**
   - Compare mental health vs non-mental health response times

## Next Steps

1. [ ] First: Query the API for ALL available years (remove the 10,000 limit? or paginate?)
2. [ ] Find the date range of the dataset
3. [ ] Calculate response times for each record
4. [ ] Build the trend charts
5. [ ] Add filtering options (by year, district, priority)

## Tools

- Observable (browser-based) OR
- Local HTML file (already created at `docs/911/index.html`)
- Austin SODA API (already working)

## Questions to Answer First

- How many total records are in the dataset? (not just 10,000)
- How many years does it cover?
- Can we pull ALL records or do we need to paginate?
- Should we aggregate by month/day to reduce data size?
```
Now, let me also update the existing dashboard to reflect this new direction instead of the current simple view.

```tool
TOOL_NAME: edit_existing_file
BEGIN_ARG: filepath
"docs/911/index.html"