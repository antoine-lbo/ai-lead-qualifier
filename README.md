# AI Lead Qualifier

[![CI](https://github.com/antoine-lbo/ai-lead-qualifier/actions/workflows/ci.yml/badge.svg)](https://github.com/antoine-lbo/ai-lead-qualifier/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com)
[![Redis](https://img.shields.io/badge/Redis-7.0+-DC382D.svg)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](https://www.docker.com)

An intelligent lead qualification pipeline that scores and routes inbound leads in under 30 seconds using OpenAI GPT-4 and custom business rules. Built for sales teams that want to focus on high-value prospects.

## Architecture

```
Inbound Lead --> Enrichment --> AI Scoring --> Routing --> CRM Update
    |              |              |            |            |
  Webhook      Clearbit/       GPT-4       Slack      HubSpot/
  or API       LinkedIn      Analysis   Notification  Salesforce
```

## Performance

| Metric | Result |
|--------|--------|
| Qualification accuracy | 94.2% |
| Average processing time | < 28 seconds |
| Leads processed/day | 2,500+ |
| False positive rate | < 3% |

## Features

- **AI-Powered Scoring** -- GPT-4 analyzes company fit, intent signals, and budget indicators
- **Real-Time Enrichment** -- Pulls company data from Clearbit, LinkedIn, and public sources
- **Custom Scoring Rules** -- Define ICP criteria, deal size thresholds, and industry filters
- **Smart Routing** -- Routes qualified leads to the right rep based on territory, expertise, and capacity
- **Slack Notifications** -- Instant alerts with lead summaries for hot prospects
- **CRM Integration** -- Auto-updates HubSpot or Salesforce with qualification data
- **Analytics Dashboard** -- Track conversion rates, response times, and pipeline velocity
- **Batch Processing** -- Process CSV uploads of historical leads for retroactive scoring

## Quick Start

```bash
git clone https://github.com/antoine-lbo/ai-lead-qualifier.git
cd ai-lead-qualifier
pip install -r requirements.txt
cp .env.example .env  # Add your API keys
uvicorn src.main:app --reload
```

## API

```bash
# Qualify a single lead
curl -X POST http://localhost:8000/api/qualify \
  -H "Content-Type: application/json" \
  -d '{"email": "john@acme.com", "company": "Acme Corp", "message": "Looking for automation solutions for our 50-person sales team"}'
```

```json
{
  "score": 87,
  "tier": "HOT",
  "reasoning": "Enterprise company, clear pain point, budget indicators present",
  "recommended_action": "route_to_ae",
  "enrichment": {
    "company_size": "50-200",
    "industry": "Technology",
    "estimated_revenue": "$10M-$50M"
  }
}
```

## Configuration

```yaml
# config/scoring.yaml
icp:
  company_size: [50, 10000]
  industries: ["technology", "finance", "healthcare", "e-commerce"]
  min_revenue: 1000000

scoring:
  weights:
    company_fit: 0.35
    intent_signal: 0.30
    budget_indicator: 0.20
    urgency: 0.15

routing:
  hot:  { min_score: 80, action: "route_to_ae" }
  warm: { min_score: 50, action: "add_to_nurture" }
  cold: { min_score: 0,  action: "add_to_marketing" }
```

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Security

For vulnerability reports, please see [SECURITY.md](SECURITY.md).

## License

MIT -- Antoine Batreau
