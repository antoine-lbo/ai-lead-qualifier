# Deployment Guide

This guide covers deploying AI Lead Qualifier using Docker Compose for local
development, staging environments, and production-ready setups.

## Prerequisites

- Docker Engine 24+
- Docker Compose v2
- An OpenAI API key
- (Optional) Clearbit, HubSpot, Slack API keys for full functionality

## Quick Start with Docker Compose

```bash
# Clone and configure
git clone https://github.com/antoine-lbo/ai-lead-qualifier.git
cd ai-lead-qualifier
cp .env.example .env

# Add your API keys to .env, then:
docker compose up -d

# Verify everything is running
docker compose ps
curl http://localhost:8000/health
```

The stack includes:

| Service | Port | Description |
|---------|------|-------------|
| `api` | 8000 | FastAPI application server |
| `redis` | 6379 | Rate limiting & caching |
| `postgres` | 5432 | Persistent storage for leads, analytics |
| `worker` | — | Background job processor (batch scoring) |

## Architecture

```
                    ┌─────────────┐
                    │   Nginx /    │
                    │   Traefik    │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
              ┌─────┤   API (8000)  ├─────┐
              │     └──────┬───────┘     │
              │            │             │
       ┌──────▼──┐  ┌─────▼─────┐  ┌───▼────────┐
       │  Redis   │  │ PostgreSQL │  │   Worker    │
       │  (6379)  │  │  (5432)    │  │  (Celery)   │
       └──────────┘  └───────────┘  └────────────┘
```

## Environment Variables

Create a `.env` file from the example:

```bash
cp .env.example .env
```

### Required

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key for GPT-4 scoring |
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection string |
| `SECRET_KEY` | Application secret for JWT tokens |

### Optional Integrations

| Variable | Description |
|----------|-------------|
| `CLEARBIT_API_KEY` | Company enrichment via Clearbit |
| `HUBSPOT_API_KEY` | CRM integration (HubSpot) |
| `SALESFORCE_CLIENT_ID` | CRM integration (Salesforce) |
| `SALESFORCE_CLIENT_SECRET` | Salesforce OAuth secret |
| `SLACK_WEBHOOK_URL` | Slack notifications for hot leads |
| `SLACK_BOT_TOKEN` | Slack bot for interactive alerts |

## Docker Compose Profiles

### Development (default)

```bash
docker compose up -d
```

Includes hot-reload for the API service. Source code is mounted as a volume
so changes are reflected immediately without rebuilding.

### Production

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Production overrides:
- Multi-worker Uvicorn with Gunicorn
- No volume mounts (baked-in code)
- Health checks enabled
- Restart policies (`unless-stopped`)
- Resource limits (CPU, memory)

### With monitoring

```bash
docker compose --profile monitoring up -d
```

Adds Prometheus metrics collection and a Grafana dashboard at `localhost:3000`.
## Database Setup

The PostgreSQL database is automatically initialized on first run using
`scripts/init-db.sql`. To reset the database:

```bash
# Stop and remove the database volume
docker compose down -v

# Restart — database will be recreated
docker compose up -d
```

To seed with sample data for development:

```bash
docker compose exec api python scripts/seed_db.py
```

## Scaling

### Horizontal API scaling

```bash
# Run 3 API replicas behind a load balancer
docker compose up -d --scale api=3
```

When running multiple API instances, ensure your reverse proxy (Nginx/Traefik)
distributes traffic across all replicas. Redis handles session state so any
instance can serve any request.

### Worker scaling

```bash
# Run 5 background workers for batch processing
docker compose up -d --scale worker=5
```

Each worker processes leads independently. Scale workers based on your batch
processing volume and acceptable queue latency.

## Health Checks

All services expose health endpoints:

```bash
# API health
curl http://localhost:8000/health

# Detailed health with dependency status
curl http://localhost:8000/health/detailed

# Response format:
# {
#   "status": "healthy",
#   "version": "1.2.0",
#   "uptime_seconds": 3600,
#   "dependencies": {
#     "database": "connected",
#     "redis": "connected",
#     "openai": "reachable"
#   }
# }
```

Docker Compose health checks run automatically every 30 seconds. Unhealthy
containers are restarted after 3 consecutive failures.

## Backup & Restore

### Database backup

```bash
# Create a backup
docker compose exec postgres pg_dump -U qualifier qualifier_db > backup.sql

# Restore from backup
docker compose exec -T postgres psql -U qualifier qualifier_db < backup.sql
```

### Automated daily backups

Add a cron job on the host machine:

```bash
# /etc/cron.d/qualifier-backup
0 3 * * * docker compose -f /path/to/docker-compose.yml exec -T postgres \
  pg_dump -U qualifier qualifier_db | gzip > /backups/qualifier_$(date +\%Y\%m\%d).sql.gz
```

## Reverse Proxy (Production)

For production deployments, place the API behind a reverse proxy.

### Nginx example

```nginx
upstream qualifier_api {
    server 127.0.0.1:8000;
}

server {
    listen 443 ssl http2;
    server_name api.yourcompany.com;

    ssl_certificate     /etc/letsencrypt/live/api.yourcompany.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourcompany.com/privkey.pem;

    location / {
        proxy_pass http://qualifier_api;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Rate limiting at the proxy level
    limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
    limit_req zone=api burst=20 nodelay;
}
```

## Troubleshooting

### Common issues

**Container keeps restarting**

```bash
# Check logs for the failing container
docker compose logs api --tail 50
docker compose logs worker --tail 50
```

**Database connection refused**

The API waits for PostgreSQL to be ready, but on slow machines the health
check might time out. Increase the `start_period` in `docker-compose.yml`:

```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U qualifier"]
  interval: 5s
  timeout: 5s
  retries: 10
  start_period: 30s  # Increase this
```

**OpenAI rate limiting**

If you see 429 errors from OpenAI, reduce the worker concurrency or add
a delay between requests in `config/scoring.yaml`:

```yaml
openai:
  max_concurrent_requests: 5
  request_delay_ms: 200
```

## Upgrading

```bash
# Pull latest changes
git pull origin main

# Rebuild and restart
docker compose build --no-cache
docker compose up -d

# Run any new database migrations
docker compose exec api python -m alembic upgrade head
```
