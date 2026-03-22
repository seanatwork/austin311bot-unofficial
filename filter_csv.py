import csv
from datetime import datetime, timedelta

input_file = r"C:\Users\Sean\Documents\VSCode Projects\bike lane karen\311_Service_Requests_-_Austin_Transportation_and_Public_Works_20260322.csv"
output_file = r"C:\Users\Sean\Documents\VSCode Projects\bike lane karen\311_Service_Requests_Last_365_Days.csv"

# Calculate cutoff date (365 days ago from today)
today = datetime.now()
cutoff_date = today - timedelta(days=365)

print(f"Today: {today.strftime('%Y-%m-%d')}")
print(f"Cutoff date: {cutoff_date.strftime('%Y-%m-%d')}")

filtered_rows = []
header = None
skipped = 0
kept = 0

with open(input_file, 'r', encoding='utf-8') as f:
    reader = csv.reader(f)
    header = next(reader)
    
    # Find the "Created Date" column index
    created_date_idx = header.index("Created Date")
    print(f"Created Date column index: {created_date_idx}")
    
    for row in reader:
        if len(row) <= created_date_idx:
            skipped += 1
            continue
            
        created_date_str = row[created_date_idx].strip()
        
        if not created_date_str:
            skipped += 1
            continue
        
        try:
            # Parse date format: "2022 Mar 29 03:48:47 PM"
            created_date = datetime.strptime(created_date_str, "%Y %b %d %I:%M:%S %p")
            
            if created_date >= cutoff_date:
                filtered_rows.append(row)
                kept += 1
            else:
                skipped += 1
        except ValueError as e:
            print(f"Error parsing date: {created_date_str} - {e}")
            skipped += 1

# Write filtered rows to new CSV
with open(output_file, 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(header)
    writer.writerows(filtered_rows)

print(f"\nResults:")
print(f"  Rows kept (last 365 days): {kept}")
print(f"  Rows skipped (older): {skipped}")
print(f"  Output file: {output_file}")
