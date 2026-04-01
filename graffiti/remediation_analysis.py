#!/usr/bin/env python3
"""
Graffiti Remediation Time Analysis

Calculate how quickly graffiti reports are resolved from open to close status
based on different time periods and patterns.
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import statistics

from .config import Config

logger = logging.getLogger(__name__)


class GraffitiRemediationAnalyzer:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(Config.get_db_path())
        self.service_code = Config.SERVICE_CODE
    
    def get_graffiti_with_dates(self, days_back: int = 90) -> list:
        """Get graffiti records with date information
        
        Args:
            days_back: Number of days to look back (1-365)
            
        Returns:
            List of graffiti records with date information
        """
        # Validate input
        if days_back < Config.MIN_ANALYSIS_DAYS or days_back > Config.MAX_ANALYSIS_DAYS:
            raise ValueError(
                f"days_back must be between {Config.MIN_ANALYSIS_DAYS} and "
                f"{Config.MAX_ANALYSIS_DAYS}"
            )
        
        logger.info(f"Fetching graffiti records for last {days_back} days")
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cutoff_date = (datetime.now() - timedelta(days=days_back)).isoformat() + 'Z'
        
        cursor.execute("""
            SELECT service_request_id, requested_datetime, updated_datetime,
                   status, status_notes, address, zipcode, lat, long
            FROM open311_requests
            WHERE service_code = ? AND requested_datetime > ?
            ORDER BY requested_datetime DESC
        """, (self.service_code, cutoff_date))
        
        columns = [desc[0] for desc in cursor.description]
        records = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        conn.close()
        return records
    
    def calculate_remediation_times(self, records: list) -> dict:
        """Calculate time from open to close for each record"""
        remediation_times = []
        
        for record in records:
            if record['status'].lower() == 'closed' and record['updated_datetime']:
                try:
                    # Parse dates
                    requested_dt = datetime.fromisoformat(
                        record['requested_datetime'].replace('Z', '+00:00'))
                    updated_dt = datetime.fromisoformat(
                        record['updated_datetime'].replace('Z', '+00:00'))
                    
                    # Calculate remediation time in days
                    remediation_days = (updated_dt - requested_dt).days
                    
                    # Only include reasonable times (0-60 days)
                    if 0 <= remediation_days <= 60:
                        remediation_times.append({
                            'service_request_id': record['service_request_id'],
                            'address': record['address'],
                            'requested_date': record['requested_datetime'],
                            'closed_date': record['updated_datetime'],
                            'remediation_days': remediation_days,
                            'status_notes': record['status_notes']
                        })
                        
                except (ValueError, TypeError):
                    continue
        
        return remediation_times
    
    def analyze_remediation_patterns(self, remediation_times: list) -> dict:
        """Analyze patterns in remediation times"""
        if not remediation_times:
            return {}
        
        # Basic statistics
        days_list = [r['remediation_days'] for r in remediation_times]
        
        analysis = {
            'total_closed': len(remediation_times),
            'average_days': statistics.mean(days_list) if days_list else 0,
            'median_days': statistics.median(days_list) if days_list else 0,
            'min_days': min(days_list) if days_list else 0,
            'max_days': max(days_list) if days_list else 0,
            'std_dev': statistics.stdev(days_list) if len(days_list) > 1 else 0
        }
        
        # Distribution by time ranges
        time_ranges = {
            'same_day': 0,      # Closed same day
            '1-3_days': 0,      # Quick closure
            '4-7_days': 0,      # Week closure
            '8-14_days': 0,     # 2 weeks
            '15-30_days': 0,     # 2-4 weeks
            '30+_days': 0        # Over a month
        }
        
        for remediation in remediation_times:
            days = remediation['remediation_days']
            if days == 0:
                time_ranges['same_day'] += 1
            elif days <= 3:
                time_ranges['1-3_days'] += 1
            elif days <= 7:
                time_ranges['4-7_days'] += 1
            elif days <= 14:
                time_ranges['8-14_days'] += 1
            elif days <= 30:
                time_ranges['15-30_days'] += 1
            else:
                time_ranges['30+_days'] += 1
        
        analysis['time_distribution'] = time_ranges
        return analysis
    
    def analyze_by_period(self, days_back: int = 90) -> dict:
        """Analyze remediation for specific time period"""
        records = self.get_graffiti_with_dates(days_back)
        remediation_times = self.calculate_remediation_times(records)
        analysis = self.analyze_remediation_patterns(remediation_times)
        
        return {
            'period_days': days_back,
            'total_records': len(records),
            'closed_records': analysis['total_closed'],
            'remediation_analysis': analysis,
            'sample_cases': remediation_times[:5]
        }
    
    def compare_periods(self) -> dict:
        """Compare remediation times across different periods"""
        periods = [30, 60, 90, 180, 365]
        comparisons = {}
        
        for days in periods:
            analysis = self.analyze_by_period(days)
            comparisons[f'{days}_days'] = analysis
        
        return comparisons
    
    def format_remediation_report(self, analysis: dict) -> str:
        """Format remediation analysis into readable report"""
        report = []
        report.append(f"🕒 GRAFFITI REMEDIATION TIME ANALYSIS")
        report.append("=" * 60)
        report.append(f"📅 Period: Last {analysis['period_days']} days")
        report.append(f"📊 Total Records: {analysis['total_records']:,}")
        report.append(f"✅ Closed Records: {analysis['closed_records']:,}")
        
        if analysis['closed_records'] > 0:
            remed = analysis['remediation_analysis']
            
            report.append(f"\n⏱️ Remediation Statistics:")
            report.append(f"   Average time: {remed['average_days']:.1f} days")
            report.append(f"   Median time: {remed['median_days']:.1f} days")
            report.append(f"   Fastest: {remed['min_days']} days")
            report.append(f"   Slowest: {remed['max_days']} days")
            
            if remed['std_dev'] > 0:
                report.append(f"   Consistency: ±{remed['std_dev']:.1f} days")
            
            # Time distribution
            report.append(f"\n📈 Time Distribution:")
            dist = remed['time_distribution']
            total = analysis['closed_records']
            
            report.append(f"   Same day: {dist['same_day']} ({dist['same_day']/total*100:.1f}%)")
            report.append(f"   1-3 days: {dist['1-3_days']} ({dist['1-3_days']/total*100:.1f}%)")
            report.append(f"   4-7 days: {dist['4-7_days']} ({dist['4-7_days']/total*100:.1f}%)")
            report.append(f"   8-14 days: {dist['8-14_days']} ({dist['8-14_days']/total*100:.1f}%)")
            report.append(f"   15-30 days: {dist['15-30_days']} ({dist['15-30_days']/total*100:.1f}%)")
            report.append(f"   30+ days: {dist['30+_days']} ({dist['30+_days']/total*100:.1f}%)")
            
            # Sample cases
            if analysis.get('sample_cases'):
                report.append(f"\n📋 Sample Remediation Cases:")
                for i, case in enumerate(analysis['sample_cases'], 1):
                    report.append(f"   {i}. {case['remediation_days']} days")
                    report.append(f"      📍 {case['address']}")
                    report.append(f"      📅 {case['requested_date']} → {case['closed_date']}")
                    if case['status_notes'] and len(case['status_notes']) < 100:
                        report.append(f"      📝 {case['status_notes']}")
        
        return "\n".join(report)
    
    def format_comparison_report(self, comparisons: dict) -> str:
        """Format comparison across different time periods"""
        report = []
        report.append("📊 REMEDIATION TIME COMPARISON")
        report.append("=" * 60)
        
        periods = sorted([int(k.split('_')[0]) for k in comparisons.keys()])
        
        report.append(f"{'Period':<10} | {'Closed':<8} | {'Avg Days':<10} | {'Median':<8} | {'Fastest':<8} | {'Slowest':<8}")
        report.append("-" * 65)
        
        for period in periods:
            key = f'{period}_days'
            if key in comparisons:
                comp = comparisons[key]
                report.append(f"{period:<8} days | {comp['closed_records']:<8,} | "
                            f"{comp['remediation_analysis']['average_days']:<10.1f} | "
                            f"{comp['remediation_analysis']['median_days']:<8.1f} | "
                            f"{comp['remediation_analysis']['min_days']:<8} | "
                            f"{comp['remediation_analysis']['max_days']:<8}")
        
        return "\n".join(report)

# Command Functions
def remediation_command(days_back: int = 90) -> str:
    """Show remediation analysis for specific period"""
    try:
        analyzer = GraffitiRemediationAnalyzer()
        analysis = analyzer.analyze_by_period(days_back)
        return analyzer.format_remediation_report(analysis)
    except ValueError as e:
        logger.warning(f"Invalid input: {e}")
        return f"❌ Invalid input: {e}"
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        return "❌ Database error. Please check if the database exists."
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return f"❌ An error occurred: {e}"

def compare_command() -> str:
    """Compare remediation times across multiple periods"""
    analyzer = GraffitiRemediationAnalyzer()
    comparisons = analyzer.compare_periods()
    return analyzer.format_comparison_report(comparisons)

def trends_command() -> str:
    """Show remediation trends and patterns"""
    analyzer = GraffitiRemediationAnalyzer()
    
    # Get last 6 months for trend analysis
    records = analyzer.get_graffiti_with_dates(180)
    
    if not records:
        return "📝 No graffiti data available for trend analysis."
    
    # Group by month
    monthly_data = defaultdict(list)
    
    for record in records:
        if record['status'].lower() == 'closed' and record['updated_datetime']:
            try:
                requested_dt = datetime.fromisoformat(
                    record['requested_datetime'].replace('Z', '+00:00'))
                updated_dt = datetime.fromisoformat(
                    record['updated_datetime'].replace('Z', '+00:00'))
                
                remediation_days = (updated_dt - requested_dt).days
                if 0 <= remediation_days <= 60:
                    month = requested_dt.strftime('%Y-%m')
                    monthly_data[month].append(remediation_days)
            except:
                continue
    
    if not monthly_data:
        return "📝 No closed graffiti cases found for trend analysis."
    
    # Calculate monthly averages
    report = ["📈 REMEDIATION TRENDS (Last 6 Months)"]
    report.append("=" * 50)
    
    for month in sorted(monthly_data.keys()):
        days_list = monthly_data[month]
        if days_list:
            avg_days = statistics.mean(days_list)
            count = len(days_list)
            month_name = datetime.strptime(month + '-01', '%Y-%m-%d').strftime('%B %Y')
            
            report.append(f"{month_name}: {count} cases, avg {avg_days:.1f} days")
    
    # Overall trend
    all_days = []
    for days_list in monthly_data.values():
        all_days.extend(days_list)
    
    if all_days:
        overall_avg = statistics.mean(all_days)
        report.append(f"\n📊 Overall Average: {overall_avg:.1f} days")
        
        # Trend detection
        months = sorted(monthly_data.keys())
        if len(months) >= 2:
            first_half = months[:len(months)//2]
            second_half = months[len(months)//2:]
            
            first_avg = statistics.mean([statistics.mean(monthly_data[m]) for m in first_half if monthly_data[m]])
            second_avg = statistics.mean([statistics.mean(monthly_data[m]) for m in second_half if monthly_data[m]])
            
            if second_avg < first_avg:
                report.append(f"📉 Improving trend: Faster remediation over time")
            elif second_avg > first_avg:
                report.append(f"📈 Declining trend: Slower remediation over time")
            else:
                report.append(f"📊 Stable trend: Consistent remediation time")
    
    return "\n".join(report)

def help_command() -> str:
    """Show help for remediation analysis"""
    help_text = """
🕒 GRAFFITI REMEDIATION ANALYSIS HELP

📊 ANALYSIS COMMANDS:
/remediation [days] - Remediation time analysis (default: 90 days)
/compare - Compare multiple time periods
/trends - Show 6-month remediation trends

📊 EXAMPLES:
/remediation 30 - Last 30 days remediation analysis
/remediation 180 - Last 6 months analysis
/compare - Compare 30, 60, 90, 180 day periods
/trends - Show improvement/decline patterns

💡 METRICS CALCULATED:
• Average remediation time
• Median time to closure
• Fastest/slowest cases
• Time distribution ranges
• Period-over-period comparisons
• Monthly trend analysis

📈 TIME RANGES:
• Same day: Closed within 24 hours
• 1-3 days: Quick remediation
• 4-7 days: Standard week
• 8-14 days: 2-week period
• 15-30 days: 2-4 weeks
• 30+ days: Extended cases
"""
    return help_text.strip()

# Main Interface
def handle_command(command: str, args: list) -> str:
    """Handle remediation analysis commands"""
    command = command.lower().lstrip('/')
    
    if command in ['remediation', 'remedy']:
        days = int(args[0]) if args and args[0].isdigit() else 90
        return remediation_command(days)
    
    elif command == 'compare':
        return compare_command()
    
    elif command in ['trends', 'trend']:
        return trends_command()
    
    elif command in ['help', 'h']:
        return help_command()
    
    else:
        return "❓ Unknown command. Type /help for available commands."

if __name__ == "__main__":
    # Demo the remediation analysis
    print("🕒 Graffiti Remediation Analysis Demo")
    print("=" * 50)
    
    # Test main commands
    print("\n1. 90-Day Remediation Analysis:")
    print(remediation_command(90))
    
    print("\n" + "="*50)
    print("\n2. Period Comparison:")
    print(compare_command())
    
    print("\n" + "="*50)
    print("\n3. 6-Month Trends:")
    print(trends_command())
    
    print("\n" + "="*50)
    print("\n4. Help Information:")
    print(help_command())
