// SODA v3 method for Observable
url = "https://data.austintexas.gov/api/v3/views/22de-7rzg/query.json"
AppToken = "cnFgw7cGvmoKE5g3WUMbkpGrDMBH-YeGp97N"

// v3 uses a JSON body instead of URL parameters
query_body = {
  $query: {
    "where": "incident_type like '%Dispatched%'",
    "order": "response_datetime DESC",
    "limit": 10000,
    "select": "incident_number, incident_type, priority_level, response_datetime, first_unit_arrived_datetime, call_closed_datetime, council_district, mental_health_flag, initial_problem_category"
  }
}

data = fetch(url, {
  method: "POST",
  headers: {
    "X-App-Token": AppToken,
    "Content-Type": "application/json"
  },
  body: JSON.stringify(query_body)
}).then(r => r.json())

data[0] // Preview first record