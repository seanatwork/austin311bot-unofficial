#!/usr/bin/env python3
"""
Graffiti Analysis Bot

Focuses on analysis, heatmapping, and pattern detection with photo support as secondary feature.
Based on analysis of 404 graffiti records from Austin 311 system.
"""

import sqlite3
import json
import re
import logging
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from typing import List, Dict, Optional, Tuple

from .config import Config

logger = logging.getLogger(__name__)


class GraffitiAnalysisBot:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(Config.get_db_path())
        self.service_code = Config.SERVICE_CODE
        
    def get_graffiti_data(self, days_back: int = 90) -> List[Dict]:
        """Get graffiti data from database
        
        Args:
            days_back: Number of days to look back (1-365)
            
        Returns:
            List of graffiti records
            
        Raises:
            ValueError: If days_back is out of valid range
        """
        # Validate input
        if days_back < Config.MIN_ANALYSIS_DAYS or days_back > Config.MAX_ANALYSIS_DAYS:
            raise ValueError(
                f"days_back must be between {Config.MIN_ANALYSIS_DAYS} and "
                f"{Config.MAX_ANALYSIS_DAYS}"
            )
        
        logger.info(f"Fetching graffiti data for last {days_back} days")
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cutoff_date = (datetime.now() - timedelta(days=days_back)).isoformat() + 'Z'
        
        cursor.execute("""
            SELECT service_request_id, requested_datetime, updated_datetime,
                   status, status_notes, address, zipcode, lat, long,
                   media_url, attributes_json, extended_attributes_json
            FROM open311_requests
            WHERE service_code = ? AND requested_datetime > ?
            ORDER BY requested_datetime DESC
        """, (self.service_code, cutoff_date))
        
        columns = [desc[0] for desc in cursor.description]
        records = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        conn.close()
        return records
    
    def analyze_patterns(self, records: List[Dict]) -> Dict:
        """Analyze patterns in graffiti data"""
        if not records:
            return {}
        
        analysis = {
            'total_records': len(records),
            'status_distribution': Counter(r['status'] for r in records),
            'temporal_patterns': self.analyze_temporal_patterns(records),
            'geographic_patterns': self.analyze_geographic_patterns(records),
            'address_patterns': self.analyze_address_patterns(records),
            'status_notes_patterns': self.analyze_status_notes(records),
            'media_analysis': self.analyze_media_attachments(records)
        }
        
        return analysis
    
    def analyze_temporal_patterns(self, records: List[Dict]) -> Dict:
        """Analyze temporal patterns"""
        dates = []
        for record in records:
            try:
                dt = datetime.fromisoformat(record['requested_datetime'].replace('Z', '+00:00'))
                dates.append(dt)
            except:
                continue
        
        if not dates:
            return {}
        
        return {
            'busiest_day': max(Counter(dt.weekday() for dt in dates).items(), key=lambda x: x[1])[0],
            'busiest_hour': max(Counter(dt.hour for dt in dates).items(), key=lambda x: x[1])[0],
            'recent_7_days': sum(1 for dt in dates if (datetime.now(dt.tzinfo) - dt).days <= 7),
            'day_names': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
            'hourly_distribution': dict(Counter(dt.hour for dt in dates))
        }
    
    def analyze_geographic_patterns(self, records: List[Dict]) -> Dict:
        """Analyze geographic patterns"""
        locations = [(r['lat'], r['long']) for r in records if r['lat'] and r['long']]
        
        if len(locations) < 2:
            return {'total_locations': len(locations)}
        
        lats = [loc[0] for loc in locations]
        lons = [loc[1] for loc in locations]
        
        return {
            'total_locations': len(locations),
            'lat_range': max(lats) - min(lats),
            'lon_range': max(lons) - min(lons),
            'center_lat': sum(lats) / len(lats),
            'center_lon': sum(lons) / len(lons),
            'hotspots': self.find_hotspots(locations)
        }
    
    def find_hotspots(self, locations: List[Tuple], threshold: float = 0.001) -> List[Dict]:
        """Find geographic hotspots using simple clustering"""
        if len(locations) < 3:
            return []
        
        # Simple density-based clustering
        clusters = defaultdict(list)
        
        for lat, lon in locations:
            # Round to create clusters
            cluster_key = (round(lat/threshold), round(lon/threshold))
            clusters[cluster_key].append((lat, lon))
        
        hotspots = []
        for cluster_points in clusters.values():
            if len(cluster_points) >= 3:  # Minimum cluster size
                avg_lat = sum(p[0] for p in cluster_points) / len(cluster_points)
                avg_lon = sum(p[1] for p in cluster_points) / len(cluster_points)
                hotspots.append({
                    'center_lat': avg_lat,
                    'center_lon': avg_lon,
                    'count': len(cluster_points),
                    'points': cluster_points
                })
        
        return sorted(hotspots, key=lambda x: x['count'], reverse=True)
    
    def analyze_address_patterns(self, records: List[Dict]) -> Dict:
        """Analyze address patterns"""
        addresses = [r['address'] for r in records if r['address']]
        
        intersections = [addr for addr in addresses if '&' in addr or 'and' in addr.lower()]
        street_words = []
        
        for addr in addresses:
            words = re.findall(r'\b\w+\b', addr.lower())
            street_words.extend([word for word in words if len(word) > 2])
        
        return {
            'total_addresses': len(addresses),
            'intersections': len(intersections),
            'intersection_rate': len(intersections) / len(addresses) if addresses else 0,
            'common_streets': Counter(street_words).most_common(10)
        }
    
    def analyze_status_notes(self, records: List[Dict]) -> Dict:
        """Analyze status notes for patterns"""
        notes = [r['status_notes'] for r in records if r['status_notes']]
        
        action_patterns = {
            'assigned': ['assigned', 'referred', 'forwarded'],
            'in_progress': ['in progress', 'working', 'investigating'],
            'completed': ['completed', 'resolved', 'closed', 'removed'],
            'scheduled': ['scheduled', 'planned', 'expected']
        }
        
        pattern_counts = {}
        all_text = ' '.join(notes).lower()
        
        for pattern_name, keywords in action_patterns.items():
            count = sum(1 for note in notes if any(keyword in note.lower() for keyword in keywords))
            if count > 0:
                pattern_counts[pattern_name] = count
        
        return {
            'total_notes': len(notes),
            'action_patterns': pattern_counts,
            'all_text': all_text
        }

    def analyze_media_attachments(self, records: List[Dict]) -> Dict:
        """Analyze media attachments (photos)"""
        media_urls = [r['media_url'] for r in records if r['media_url']]
        
        return {
            'total_with_media': len(media_urls),
            'media_rate': len(media_urls) / len(records) if records else 0,
            'sample_urls': media_urls[:3]  # First 3 photo URLs
        }
    
    def generate_heatmap_data(self, records: List[Dict]) -> List[Dict]:
        """Generate data for heatmap visualization"""
        heatmap_data = []
        
        for record in records:
            if record['lat'] and record['long']:
                # Weight by recency (more recent = higher intensity)
                try:
                    dt = datetime.fromisoformat(record['requested_datetime'].replace('Z', '+00:00'))
                    days_old = (datetime.now(dt.tzinfo) - dt).days
                    intensity = max(1, 10 - days_old)  # Decay over 10 days
                except:
                    intensity = 1
                
                heatmap_data.append({
                    'lat': record['lat'],
                    'lon': record['long'],
                    'intensity': intensity,
                    'status': record['status'],
                    'address': record['address'],
                    'date': record['requested_datetime']
                })
        
        return heatmap_data
    
    def generate_insights(self, analysis: Dict) -> List[str]:
        """Generate actionable insights from analysis"""
        insights = []
        
        # Status insights
        total = analysis['total_records']
        open_count = analysis['status_distribution'].get('open', 0)
        if open_count / total > 0.3:
            insights.append(f"🔴 High open rate: {open_count/total*100:.1f}% ({open_count} open reports)")
        
        # Temporal insights
        temporal = analysis['temporal_patterns']
        if temporal.get('busiest_hour') == 19:  # 7 PM
            insights.append(f"🌆 Peak reporting at 7 PM - likely after-work observations")
        
        if temporal.get('busiest_day') == 1:  # Tuesday
            insights.append(f"📅 Busiest on Tuesday - consider mid-week resource allocation")
        
        # Geographic insights
        geo = analysis['geographic_patterns']
        if geo.get('hotspots'):
            top_hotspot = geo['hotspots'][0]
            insights.append(f"🗺️ Top hotspot: {top_hotspot['count']} reports in one area")
        
        # Address insights
        addr = analysis['address_patterns']
        if addr.get('intersection_rate', 0) > 0.08:  # 8%+
            insights.append(f"🚦 High intersection rate: {addr['intersection_rate']*100:.1f}% at intersections")
        
        return insights
    
    def format_analysis_report(self, analysis: Dict) -> str:
        """Format analysis into readable report"""
        report = []
        report.append("🎨 GRAFFITI ANALYSIS REPORT")
        report.append("=" * 50)
        report.append(f"📊 Total Records: {analysis['total_records']:,}")
        
        # Status distribution
        report.append("\n📋 Status Distribution:")
        for status, count in analysis['status_distribution'].most_common():
            percentage = count / analysis['total_records'] * 100
            report.append(f"   {status}: {count:,} ({percentage:.1f}%)")
        
        # Temporal patterns
        temporal = analysis['temporal_patterns']
        if temporal:
            report.append(f"\n⏰ Temporal Patterns:")
            day_names = temporal.get('day_names', ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
            report.append(f"   Busiest day: {day_names[temporal.get('busiest_day', 0)]}")
            report.append(f"   Busiest hour: {temporal.get('busiest_hour', 0):02d}:00")
            report.append(f"   Recent (7 days): {temporal.get('recent_7_days', 0)} reports")
        
        # Geographic patterns
        geo = analysis['geographic_patterns']
        if geo.get('hotspots'):
            report.append(f"\n🗺️ Geographic Hotspots:")
            for i, hotspot in enumerate(geo['hotspots'][:3], 1):
                report.append(f"   {i}. {hotspot['count']} reports near ({hotspot['center_lat']:.4f}, {hotspot['center_lon']:.4f})")
        
        # Address patterns
        addr = analysis['address_patterns']
        if addr:
            report.append(f"\n📍 Address Patterns:")
            report.append(f"   Total addresses: {addr['total_addresses']:,}")
            report.append(f"   Intersections: {addr['intersections']} ({addr['intersection_rate']*100:.1f}%)")
            if addr.get('common_streets'):
                report.append(f"   Common streets: {', '.join([word for word, count in addr['common_streets'][:5]])}")
        
        # Media attachments
        media = analysis.get('media_analysis')
        if media and media['total_with_media'] > 0:
            report.append(f"\n📸 Photo Attachments:")
            report.append(f"   Reports with photos: {media['total_with_media']} ({media['media_rate']*100:.1f}%)")
            if media.get('sample_urls'):
                report.append(f"   Sample photos:")
                for url in media['sample_urls']:
                    report.append(f"      • {url}")
        
        # Insights
        insights = self.generate_insights(analysis)
        if insights:
            report.append(f"\n💡 Key Insights:")
            for insight in insights:
                report.append(f"   {insight}")
        
        return "\n".join(report)

# Bot Command Functions
def analyze_graffiti_command(days_back: int = 90) -> str:
    """Main analysis command"""
    try:
        bot = GraffitiAnalysisBot()
        records = bot.get_graffiti_data(days_back)

        if not records:
            return (
                "📝 No graffiti data found for analysis.\n\n"
                "💡 Try:\n"
                "  • Use a longer time period: `/analyze 180`\n"
                "  • Run data ingestion: `python ingest_graffiti_data.py`"
            )

        analysis = bot.analyze_patterns(records)
        return bot.format_analysis_report(analysis)
    except ValueError as e:
        logger.warning(f"Invalid input: {e}")
        return f"❌ Invalid input: {e}"
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        return "❌ Database error. Please check if the database exists."
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return f"❌ An error occurred: {e}"

def get_hotspot_clusters(days_back: int = 90) -> list:
    """Return top graffiti clusters as list of dicts with lat/lon/count/addresses."""
    bot = GraffitiAnalysisBot()
    records = bot.get_graffiti_data(days_back)
    if not records:
        return []
    geo = bot.analyze_geographic_patterns(records)
    hotspots = geo.get("hotspots", [])
    result = []
    for hotspot in hotspots[:8]:
        nearby = [
            r["address"] for r in records
            if r["lat"] and r["long"]
            and abs(r["lat"] - hotspot["center_lat"]) < 0.001
            and abs(r["long"] - hotspot["center_lon"]) < 0.001
            and r["address"]
        ]
        result.append({
            "lat": hotspot["center_lat"],
            "lon": hotspot["center_lon"],
            "count": hotspot["count"],
            "sample_address": nearby[0] if nearby else None,
        })
    return result


def hotspot_command() -> str:
    """Show graffiti hotspots"""
    bot = GraffitiAnalysisBot()
    records = bot.get_graffiti_data(90)
    
    if not records:
        return "📝 No graffiti data available."
    
    geo_analysis = bot.analyze_geographic_patterns(records)
    hotspots = geo_analysis.get('hotspots', [])
    
    if not hotspots:
        return "🗺️ No significant hotspots found."
    
    response = ["🗺️ GRAFFITI HOTSPOTS"]
    response.append("=" * 30)
    
    for i, hotspot in enumerate(hotspots[:5], 1):
        response.append(f"{i}. {hotspot['count']} reports clustered")
        response.append(f"   📍 Location: ({hotspot['center_lat']:.4f}, {hotspot['center_lon']:.4f})")
        
        # Find nearby addresses
        nearby_records = [r for r in records if r['lat'] and r['long'] and 
                        abs(r['lat'] - hotspot['center_lat']) < 0.001 and 
                        abs(r['long'] - hotspot['center_lon']) < 0.001]
        if nearby_records:
            addresses = [r['address'] for r in nearby_records[:3] if r['address']]
            if addresses:
                response.append(f"   🏠 Near: {', '.join(addresses[:2])}")
    
    return "\n".join(response)

def patterns_command(days_back: int = 30) -> str:
    """Show recent patterns"""
    bot = GraffitiAnalysisBot()
    records = bot.get_graffiti_data(days_back)
    
    if not records:
        return "📝 No recent graffiti data found."
    
    temporal = bot.analyze_temporal_patterns(records)
    
    response = ["📈 RECENT GRAFFITI PATTERNS"]
    response.append("=" * 35)
    response.append(f"📅 Period: Last {days_back} days")
    response.append(f"📊 Total reports: {len(records)}")
    
    if temporal.get('hourly_distribution'):
        response.append("\n⏰ Hourly Distribution:")
        for hour in range(6, 24, 2):  # Every 2 hours
            count = temporal['hourly_distribution'].get(hour, 0)
            bar = "█" * min(20, count)  # Simple bar chart
            response.append(f"   {hour:02d}:00 {bar} {count}")
    
    return "\n".join(response)

def help_command() -> str:
    """Show help information"""
    help_text = """
🎨 GRAFFITI ANALYSIS BOT HELP

📊 ANALYSIS COMMANDS:
/analyze [days] - Full graffiti analysis (default: 90 days)
/hotspot - Show geographic hotspots  
/patterns [days] - Recent temporal patterns (default: 30 days)

📸 REPORTING COMMANDS (Secondary):
/report [location] [description] - File graffiti report with photo support

📊 EXAMPLES:
/analyze - 90-day graffiti analysis
/hotspot - Show all hotspots
/patterns 14 - Last 2 weeks patterns
/report "123 Main St" "Large tag on wall"

💡 FEATURES:
• Focuses on analysis and heatmapping
• Photo upload support (JPEG, PNG, GIF, BMP, TIF, PDF)
• Real-time pattern detection
• Geographic hotspot identification
• Temporal trend analysis
• Status tracking integration
"""
    return help_text.strip()

# Main Bot Interface
def handle_command(command: str, args: List[str]) -> str:
    """Handle bot commands"""
    command = command.lower().lstrip('/')
    
    if command in ['analyze', 'analysis']:
        days = int(args[0]) if args and args[0].isdigit() else 90
        return analyze_graffiti_command(days)
    
    elif command in ['hotspot', 'hotspots']:
        return hotspot_command()
    
    elif command in ['patterns', 'pattern']:
        days = int(args[0]) if args and args[0].isdigit() else 30
        return patterns_command(days)
    
    elif command in ['help', 'h']:
        return help_command()
    
    else:
        return "❓ Unknown command. Type /help for available commands."

if __name__ == "__main__":
    # Demo the bot functionality
    print("🎨 Graffiti Analysis Bot Demo")
    print("=" * 40)
    
    # Test main commands
    print("\n1. Full Analysis (90 days):")
    print(analyze_graffiti_command(90))
    
    print("\n" + "="*50)
    print("\n2. Hotspot Analysis:")
    print(hotspot_command())
    
    print("\n" + "="*50)
    print("\n3. Recent Patterns (30 days):")
    print(patterns_command(30))
    
    print("\n" + "="*50)
    print("\n4. Help Information:")
    print(help_command())
