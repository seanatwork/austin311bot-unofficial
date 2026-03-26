import sqlite3
from datetime import datetime

conn = sqlite3.connect('bicycle_complaints.db')
cursor = conn.cursor()

# Get all tickets from Jan 1, 2026 to today (2026-03-25)
cursor.execute('SELECT ticket_number, created_date, address, description, status FROM bicycle_complaints WHERE created_date >= "2026-01-01T00:00:00" AND created_date <= "2026-03-25T23:59:59" ORDER BY created_date DESC')
results = cursor.fetchall()

print(f'All {len(results)} tickets submitted between Jan 1, 2026 and today:')
print('=' * 80)

for i, row in enumerate(results, 1):
    ticket_num, created_date, address, description, status = row
    print(f'{i}. {ticket_num} - {status}')
    print(f'   Date: {created_date}')
    print(f'   Address: {address}')
    desc = description[:150] + '...' if len(description) > 150 else description
    print(f'   Description: {desc}')
    print()

conn.close()
