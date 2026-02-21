"""
Database Seeder for AI Lead Qualifier

Populates the database with realistic sample data for development
and testing. Generates leads, qualification results, and analytics
data with configurable volume.

Usage:
    python scripts/seed_db.py              # Default: 100 leads
    python scripts/seed_db.py --count 500  # Custom count
    python scripts/seed_db.py --reset      # Clear and reseed
    python scripts/seed_db.py --export csv  # Export seed data
"""

import argparse
import asyncio
import csv
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings


# ---------------------------------------------------------------------------
# Sample Data Pools
# ---------------------------------------------------------------------------

COMPANIES = [
    {"name": "Acme Corp", "domain": "acme.com", "size": "200-500", "industry": "Technology", "revenue": "$50M-$100M"},
    {"name": "GlobalTech Solutions", "domain": "globaltech.io", "size": "500-1000", "industry": "Technology", "revenue": "$100M-$500M"},
    {"name": "HealthFirst Inc", "domain": "healthfirst.com", "size": "1000-5000", "industry": "Healthcare", "revenue": "$500M-$1B"},
    {"name": "FinanceHub", "domain": "financehub.com", "size": "100-200", "industry": "Finance", "revenue": "$10M-$50M"},
    {"name": "EduLearn Platform", "domain": "edulearn.io", "size": "50-100", "industry": "Education", "revenue": "$5M-$10M"},
    {"name": "RetailMax", "domain": "retailmax.com", "size": "2000-5000", "industry": "E-commerce", "revenue": "$200M-$500M"},
    {"name": "CloudNine Systems", "domain": "cloudnine.dev", "size": "100-200", "industry": "Technology", "revenue": "$20M-$50M"},
    {"name": "MediCare Plus", "domain": "medicareplus.org", "size": "500-1000", "industry": "Healthcare", "revenue": "$50M-$100M"},
    {"name": "DataStream Analytics", "domain": "datastream.ai", "size": "50-200", "industry": "Technology", "revenue": "$10M-$50M"},
    {"name": "SecureVault", "domain": "securevault.io", "size": "200-500", "industry": "Cybersecurity", "revenue": "$30M-$100M"},
    {"name": "GreenEnergy Co", "domain": "greenenergy.com", "size": "1000-5000", "industry": "Energy", "revenue": "$100M-$500M"},
    {"name": "LogiChain", "domain": "logichain.io", "size": "200-500", "industry": "Logistics", "revenue": "$50M-$200M"},
    {"name": "SmartHome Labs", "domain": "smarthome.dev", "size": "50-100", "industry": "IoT", "revenue": "$5M-$20M"},
    {"name": "TravelWise", "domain": "travelwise.com", "size": "100-500", "industry": "Travel", "revenue": "$20M-$100M"},
    {"name": "FoodTech Inc", "domain": "foodtech.io", "size": "200-500", "industry": "Food & Beverage", "revenue": "$30M-$80M"},
];

FIRST_NAMES = [
    "James", "Sarah", "Michael", "Emily", "David", "Jessica", "Robert",
    "Ashley", "William", "Amanda", "Daniel", "Stephanie", "Christopher",
    "Nicole", "Matthew", "Jennifer", "Andrew", "Elizabeth", "Ryan", "Lauren",
];

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Thomas",
    "Taylor", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White",
];

TITLES = [
    "CEO", "CTO", "VP of Engineering", "Head of Sales", "Director of Marketing",
    "VP of Operations", "Chief Revenue Officer", "Product Manager",
    "Engineering Manager", "Head of Growth", "Director of IT",
    "Chief Data Officer", "VP of Business Development", "Founder",
];

MESSAGES = [
    "Looking for an automation solution for our {size}-person sales team. Currently spending {hours} hours/week on manual lead qualification.",
    "We need to improve our lead response time. Currently takes us {hours} hours to follow up with inbound leads.",
    "Interested in AI-powered lead scoring. Our team processes about {volume} leads per month and we\'re losing deals due to slow response.",
    "Want to integrate lead qualification with our {crm}. Need something that can handle {volume}+ leads/month.",
    "Exploring automation tools for our sales pipeline. Budget is around ${budget}k for this quarter.",
    "Our current lead scoring is manual and inconsistent. Need a scalable solution for our growing team of {size} reps.",
    "Heard about your AI qualifier from {referral}. Would love to see a demo for our {industry} use case.",
    "We\'re scaling rapidly and need to automate lead qualification. Currently at {volume} inbound leads/month.",
    "Looking to reduce our sales cycle. Currently {days} days from first touch to close.",
    "Need better lead routing. Our {size}-person team is not efficiently distributed across territories.",
];
SOURCES = ["website_form", "linkedin", "referral", "cold_email", "webinar", "trade_show", "partner", "organic_search"];
CRMS = ["HubSpot", "Salesforce", "Pipedrive", "Close.io"];
REFERRALS = ["a colleague", "LinkedIn", "G2 review", "a podcast", "a conference talk"];


# ---------------------------------------------------------------------------
# Lead Generator
# ---------------------------------------------------------------------------

def generate_lead(index: int) -> dict[str, Any]:
    """Generate a single realistic lead record."""
    company = random.choice(COMPANIES)
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    title = random.choice(TITLES)
    source = random.choice(SOURCES)

    # Generate message with realistic placeholders
    message_template = random.choice(MESSAGES)
    message = message_template.format(
        size=random.choice(["10", "25", "50", "100", "200"]),
        hours=random.choice(["2", "4", "8", "12", "20"]),
        volume=random.choice(["500", "1000", "2500", "5000", "10000"]),
        crm=random.choice(CRMS),
        budget=random.choice(["10", "25", "50", "100", "250"]),
        days=random.choice(["14", "21", "30", "45", "60"]),
        referral=random.choice(REFERRALS),
        industry=company["industry"],
    )

    # Weighted score distribution: more warm leads than hot/cold
    score_weights = [(0, 30, 0.2), (30, 60, 0.35), (60, 85, 0.3), (85, 100, 0.15)]
    rand = random.random()
    cumulative = 0
    score = 50
    for low, high, weight in score_weights:
        cumulative += weight
        if rand <= cumulative:
            score = random.randint(low, high)
            break

    if score >= 80:
        tier = "HOT"
    elif score >= 50:
        tier = "WARM"
    else:
        tier = "COLD"

    # Timestamp spread over the last 90 days
    days_ago = random.randint(0, 90)
    hours_offset = random.randint(0, 23)
    created_at = datetime.utcnow() - timedelta(days=days_ago, hours=hours_offset)

    return {
        "id": str(uuid.uuid4()),
        "email": f"{first.lower()}.{last.lower()}@{company[\'domain\']}",
        "first_name": first,
        "last_name": last,
        "company": company["name"],
        "company_domain": company["domain"],
        "company_size": company["size"],
        "industry": company["industry"],
        "estimated_revenue": company["revenue"],
        "title": title,
        "source": source,
        "message": message,
        "score": score,
        "tier": tier,
        "processing_time_ms": random.randint(800, 28000),
        "qualified_at": created_at.isoformat(),
        "created_at": created_at.isoformat(),
        "routing_action": {
            "HOT": "route_to_ae",
            "WARM": "add_to_nurture",
            "COLD": "add_to_marketing",
        }[tier],
        "enrichment": {
            "company_size": company["size"],
            "industry": company["industry"],
            "estimated_revenue": company["revenue"],
            "linkedin_url": f"https://linkedin.com/company/{company[\'domain\'].split(\'.\')[0]}",
            "technologies": random.sample(
                ["Python", "React", "AWS", "Kubernetes", "Terraform", "Docker",
                 "PostgreSQL", "Redis", "Node.js", "Go", "Java", "Salesforce"],
                k=random.randint(2, 5),
            ),
        },
    }

# ---------------------------------------------------------------------------
# Database Operations
# ---------------------------------------------------------------------------

async def get_connection() -> asyncpg.Connection:
    """Create a database connection."""
    database_url = os.getenv("DATABASE_URL", "postgresql://localhost:5432/lead_qualifier")
    return await asyncpg.connect(database_url)


async def reset_database(conn: asyncpg.Connection) -> None:
    """Drop and recreate all tables."""
    print("Resetting database...")
    await conn.execute("""
        DROP TABLE IF EXISTS lead_analytics CASCADE;
        DROP TABLE IF EXISTS qualification_results CASCADE;
        DROP TABLE IF EXISTS leads CASCADE;
    """)

    # Re-run init schema
    schema_path = Path(__file__).parent / "init-db.sql"
    if schema_path.exists():
        schema_sql = schema_path.read_text()
        await conn.execute(schema_sql)
        print("Schema recreated from init-db.sql")
    else:
        print("Warning: init-db.sql not found, creating tables inline")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id UUID PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                first_name VARCHAR(100),
                last_name VARCHAR(100),
                company VARCHAR(255),
                company_domain VARCHAR(255),
                title VARCHAR(255),
                source VARCHAR(50),
                message TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS qualification_results (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                lead_id UUID REFERENCES leads(id),
                score INTEGER NOT NULL,
                tier VARCHAR(10) NOT NULL,
                routing_action VARCHAR(50),
                processing_time_ms INTEGER,
                enrichment JSONB,
                qualified_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS lead_analytics (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                lead_id UUID REFERENCES leads(id),
                event_type VARCHAR(50),
                event_data JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
            CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company);
            CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
            CREATE INDEX IF NOT EXISTS idx_results_tier ON qualification_results(tier);
            CREATE INDEX IF NOT EXISTS idx_results_score ON qualification_results(score);
            CREATE INDEX IF NOT EXISTS idx_analytics_type ON lead_analytics(event_type);
        """)


async def seed_leads(conn: asyncpg.Connection, leads: list[dict]) -> None:
    """Insert generated leads into the database."""
    print(f"Seeding {len(leads)} leads...")

    for i, lead in enumerate(leads):
        # Insert lead
        await conn.execute(
            """
            INSERT INTO leads (id, email, first_name, last_name, company, company_domain, title, source, message, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (id) DO NOTHING
            """,
            uuid.UUID(lead["id"]),
            lead["email"],
            lead["first_name"],
            lead["last_name"],
            lead["company"],
            lead["company_domain"],
            lead["title"],
            lead["source"],
            lead["message"],
            datetime.fromisoformat(lead["created_at"]),
        )

        # Insert qualification result
        await conn.execute(
            """
            INSERT INTO qualification_results (lead_id, score, tier, routing_action, processing_time_ms, enrichment, qualified_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            uuid.UUID(lead["id"]),
            lead["score"],
            lead["tier"],
            lead["routing_action"],
            lead["processing_time_ms"],
            json.dumps(lead["enrichment"]),
            datetime.fromisoformat(lead["qualified_at"]),
        )

        # Generate analytics events
        events = generate_analytics_events(lead)
        for event in events:
            await conn.execute(
                """
                INSERT INTO lead_analytics (lead_id, event_type, event_data, created_at)
                VALUES ($1, $2, $3, $4)
                """,
                uuid.UUID(lead["id"]),
                event["type"],
                json.dumps(event["data"]),
                datetime.fromisoformat(event["timestamp"]),
            )

        if (i + 1) % 50 == 0:
            print(f"  Inserted {i + 1}/{len(leads)} leads...")

    print(f"Successfully seeded {len(leads)} leads with qualification results and analytics.")

def generate_analytics_events(lead: dict) -> list[dict]:
    """Generate realistic analytics events for a lead."""
    events = []
    base_time = datetime.fromisoformat(lead["created_at"])

    # Every lead gets a "received" event
    events.append({
        "type": "lead_received",
        "data": {"source": lead["source"], "email": lead["email"]},
        "timestamp": base_time.isoformat(),
    })

    # Enrichment event
    enrich_time = base_time + timedelta(seconds=random.randint(1, 5))
    events.append({
        "type": "enrichment_completed",
        "data": {"provider": random.choice(["clearbit", "linkedin", "both"]), "fields_found": random.randint(3, 12)},
        "timestamp": enrich_time.isoformat(),
    })

    # Qualification event
    qual_time = enrich_time + timedelta(seconds=random.randint(5, 25))
    events.append({
        "type": "qualification_completed",
        "data": {"score": lead["score"], "tier": lead["tier"], "processing_ms": lead["processing_time_ms"]},
        "timestamp": qual_time.isoformat(),
    })

    # Routing event
    route_time = qual_time + timedelta(seconds=random.randint(1, 3))
    events.append({
        "type": "lead_routed",
        "data": {"action": lead["routing_action"], "tier": lead["tier"]},
        "timestamp": route_time.isoformat(),
    })

    # Hot leads get additional events
    if lead["tier"] == "HOT":
        notify_time = route_time + timedelta(seconds=random.randint(1, 5))
        events.append({
            "type": "slack_notification_sent",
            "data": {"channel": "#hot-leads", "mentioned_rep": True},
            "timestamp": notify_time.isoformat(),
        })

        # Some hot leads get a CRM update
        if random.random() > 0.3:
            crm_time = notify_time + timedelta(seconds=random.randint(2, 10))
            events.append({
                "type": "crm_updated",
                "data": {"crm": random.choice(CRMS), "deal_created": random.random() > 0.5},
                "timestamp": crm_time.isoformat(),
            })

    return events


# ---------------------------------------------------------------------------
# Export Functions
# ---------------------------------------------------------------------------

def export_to_csv(leads: list[dict], output_path: str) -> None:
    """Export generated leads to a CSV file."""
    fieldnames = [
        "id", "email", "first_name", "last_name", "company",
        "company_domain", "industry", "company_size", "estimated_revenue",
        "title", "source", "message", "score", "tier", "routing_action",
        "processing_time_ms", "created_at",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leads)

    print(f"Exported {len(leads)} leads to {output_path}")


def export_to_json(leads: list[dict], output_path: str) -> None:
    """Export generated leads to a JSON file."""
    with open(output_path, "w") as f:
        json.dump(leads, f, indent=2, default=str)

    print(f"Exported {len(leads)} leads to {output_path}")


def print_summary(leads: list[dict]) -> None:
    """Print a summary of generated seed data."""
    total = len(leads)
    hot = sum(1 for l in leads if l["tier"] == "HOT")
    warm = sum(1 for l in leads if l["tier"] == "WARM")
    cold = sum(1 for l in leads if l["tier"] == "COLD")
    avg_score = sum(l["score"] for l in leads) / total if total else 0
    avg_time = sum(l["processing_time_ms"] for l in leads) / total if total else 0

    industries = {}
    for lead in leads:
        ind = lead["industry"]
        industries[ind] = industries.get(ind, 0) + 1

    sources = {}
    for lead in leads:
        src = lead["source"]
        sources[src] = sources.get(src, 0) + 1

    print("\n" + "=" * 60)
    print("SEED DATA SUMMARY")
    print("=" * 60)
    print(f"Total leads:          {total}")
    print(f"Hot leads:            {hot} ({hot/total*100:.1f}%)")
    print(f"Warm leads:           {warm} ({warm/total*100:.1f}%)")
    print(f"Cold leads:           {cold} ({cold/total*100:.1f}%)")
    print(f"Average score:        {avg_score:.1f}")
    print(f"Avg processing time:  {avg_time:.0f}ms")
    print(f"\nIndustry breakdown:")
    for ind, count in sorted(industries.items(), key=lambda x: -x[1]):
        print(f"  {ind:<20} {count:>4} ({count/total*100:.1f}%)")
    print(f"\nSource breakdown:")
    for src, count in sorted(sources.items(), key=lambda x: -x[1]):
        print(f"  {src:<20} {count:>4} ({count/total*100:.1f}%)")
    print("=" * 60)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(count: int = 100, reset: bool = False, export: str | None = None) -> None:
    """Main seeder entry point."""
    print(f"AI Lead Qualifier — Database Seeder")
    print(f"Generating {count} leads...\n")

    # Generate leads
    random.seed(42)  # Reproducible seed data
    leads = [generate_lead(i) for i in range(count)]

    # Print summary
    print_summary(leads)

    # Export if requested
    if export:
        output_dir = Path(__file__).parent.parent / "data"
        output_dir.mkdir(exist_ok=True)

        if export == "csv":
            export_to_csv(leads, str(output_dir / "seed_leads.csv"))
        elif export == "json":
            export_to_json(leads, str(output_dir / "seed_leads.json"))
        elif export == "both":
            export_to_csv(leads, str(output_dir / "seed_leads.csv"))
            export_to_json(leads, str(output_dir / "seed_leads.json"))
        return

    # Database seeding
    try:
        conn = await get_connection()
    except Exception as e:
        print(f"\nCould not connect to database: {e}")
        print("Falling back to JSON export...")
        output_dir = Path(__file__).parent.parent / "data"
        output_dir.mkdir(exist_ok=True)
        export_to_json(leads, str(output_dir / "seed_leads.json"))
        return

    try:
        if reset:
            await reset_database(conn)

        await seed_leads(conn, leads)

        # Verify counts
        lead_count = await conn.fetchval("SELECT COUNT(*) FROM leads")
        result_count = await conn.fetchval("SELECT COUNT(*) FROM qualification_results")
        event_count = await conn.fetchval("SELECT COUNT(*) FROM lead_analytics")
        print(f"\nDatabase verification:")
        print(f"  Leads:                {lead_count}")
        print(f"  Qualification results: {result_count}")
        print(f"  Analytics events:      {event_count}")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the AI Lead Qualifier database")
    parser.add_argument("--count", type=int, default=100, help="Number of leads to generate (default: 100)")
    parser.add_argument("--reset", action="store_true", help="Reset database before seeding")
    parser.add_argument("--export", choices=["csv", "json", "both"], help="Export seed data instead of inserting into DB")
    args = parser.parse_args()

    asyncio.run(main(count=args.count, reset=args.reset, export=args.export))
