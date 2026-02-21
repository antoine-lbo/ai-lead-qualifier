# API Reference

Base URL: `http://localhost:8000` (development) or your deployed domain.

All endpoints require an API key passed via the `Authorization` header:

```
Authorization: Bearer YOUR_API_KEY
```

---

## Lead Qualification

### POST /api/qualify

Score and qualify a single inbound lead using GPT-4 analysis.

**Request body:**

```json
{
  "email": "john@acme.com",
  "company": "Acme Corp",
  "name": "John Smith",
  "message": "Looking for automation solutions for our 50-person sales team",
  "source": "website_form",
  "metadata": {
    "utm_source": "google",
    "landing_page": "/enterprise"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email` | string | Yes | Lead contact email |
| `company` | string | Yes | Company name |
| `name` | string | No | Contact full name |
| `message` | string | No | Inbound message or inquiry text |
| `phone` | string | No | Phone number |
| `source` | string | No | Lead source (`website_form`, `api`, `csv`, `webhook`) |
| `metadata` | object | No | Arbitrary key-value pairs (UTM params, page info, etc.) |

**Response (200):**

```json
{
  "id": "lead_8f3k2j1",
  "score": 87,
  "tier": "HOT",
  "reasoning": "Enterprise company with clear pain point and budget indicators. 50-person team suggests mid-market deal size.",
  "recommended_action": "route_to_ae",
  "enrichment": {
    "company_size": "50-200",
    "industry": "Technology",
    "estimated_revenue": "$10M-$50M",
    "linkedin_url": "https://linkedin.com/company/acme-corp",
    "location": "San Francisco, CA"
  },
  "scoring_breakdown": {
    "company_fit": 0.92,
    "intent_signal": 0.85,
    "budget_indicator": 0.78,
    "urgency": 0.70
  },
  "processing_time_ms": 2340,
  "created_at": "2025-03-15T10:30:00Z"
}
```

**Tier definitions:**

| Tier | Score Range | Action |
|------|-----------|--------|
| `HOT` | 80-100 | Route to account executive immediately |
| `WARM` | 50-79 | Add to nurture sequence |
| `COLD` | 0-49 | Add to marketing automation |

---

## Batch Processing

### POST /api/batch/qualify

Submit a batch of leads for asynchronous qualification.

**Request body:**

```json
{
  "leads": [
    { "email": "alice@startup.io", "company": "Startup Inc" },
    { "email": "bob@bigcorp.com", "company": "BigCorp" }
  ],
  "webhook_url": "https://yourapp.com/webhook/batch-complete",
  "priority": "normal"
}
```

**Response (202):**

```json
{
  "batch_id": "batch_9x2m4k",
  "total_leads": 2,
  "status": "processing",
  "estimated_completion_seconds": 45,
  "status_url": "/api/batch/batch_9x2m4k/status"
}
```

### GET /api/batch/{batch_id}/status

Check the status of a batch processing job.

**Response (200):**

```json
{
  "batch_id": "batch_9x2m4k",
  "status": "completed",
  "total_leads": 2,
  "processed": 2,
  "failed": 0,
  "results": [
    { "email": "alice@startup.io", "score": 62, "tier": "WARM" },
    { "email": "bob@bigcorp.com", "score": 91, "tier": "HOT" }
  ],
  "processing_time_ms": 8920,
  "completed_at": "2025-03-15T10:31:00Z"
}
```
---

## Webhooks

### POST /api/webhooks

Register a webhook endpoint to receive real-time qualification events.

**Request body:**

```json
{
  "url": "https://yourapp.com/webhook/leads",
  "events": ["lead.qualified", "lead.scored", "batch.completed"],
  "secret": "whsec_your_signing_secret"
}
```

**Available events:**

| Event | Description |
|-------|-------------|
| `lead.qualified` | A lead has been scored and assigned a tier |
| `lead.scored` | Score updated (re-qualification) |
| `lead.enriched` | Enrichment data has been appended |
| `lead.routed` | Lead has been routed to a CRM or rep |
| `batch.completed` | A batch processing job has finished |
| `batch.failed` | A batch processing job has failed |

**Webhook payload format:**

```json
{
  "event": "lead.qualified",
  "timestamp": "2025-03-15T10:30:00Z",
  "data": {
    "id": "lead_8f3k2j1",
    "email": "john@acme.com",
    "score": 87,
    "tier": "HOT"
  }
}
```

Webhook payloads are signed using HMAC-SHA256. Verify with the `X-Qualifier-Signature` header.

### GET /api/webhooks

List all registered webhooks.

### DELETE /api/webhooks/{webhook_id}

Remove a registered webhook.

---

## Analytics

### GET /api/analytics

Retrieve qualification analytics for a given time period.

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `period` | string | `30d` | Time window (`7d`, `30d`, `90d`, `all`) |
| `group_by` | string | `day` | Aggregation (`hour`, `day`, `week`, `month`) |

**Response (200):**

```json
{
  "period": "30d",
  "total_leads": 2847,
  "by_tier": {
    "HOT": 342,
    "WARM": 1205,
    "COLD": 1300
  },
  "avg_score": 52.3,
  "avg_processing_time_ms": 2150,
  "conversion_rate": 0.12,
  "daily_breakdown": [
    { "date": "2025-03-14", "leads": 95, "hot": 12, "warm": 41, "cold": 42 },
    { "date": "2025-03-15", "leads": 102, "hot": 15, "warm": 38, "cold": 49 }
  ]
}
```

---

## CRM Integration

### POST /api/crm/sync

Manually trigger a CRM sync for a qualified lead.

```json
{
  "lead_id": "lead_8f3k2j1",
  "crm": "hubspot",
  "pipeline_id": "default",
  "owner_email": "rep@yourcompany.com"
}
```

Supported CRMs: `hubspot`, `salesforce`.

---

## Health & Status

### GET /health

Basic health check. Returns `200` if the API is running.

### GET /health/detailed

Detailed health check with dependency statuses (database, Redis, OpenAI).

---

## Error Responses

All errors follow a consistent format:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "email is required",
    "details": { "field": "email" }
  }
}
```

| Status | Code | Description |
|--------|------|-------------|
| 400 | `VALIDATION_ERROR` | Missing or invalid request fields |
| 401 | `UNAUTHORIZED` | Missing or invalid API key |
| 403 | `FORBIDDEN` | Insufficient permissions |
| 404 | `NOT_FOUND` | Resource not found |
| 429 | `RATE_LIMITED` | Too many requests — includes `Retry-After` header |
| 500 | `INTERNAL_ERROR` | Server error — safe to retry with backoff |
| 503 | `SERVICE_UNAVAILABLE` | Dependency down (OpenAI, database) |

---

## Rate Limits

| Plan | Requests/min | Batch size | Concurrent batches |
|------|-------------|------------|-------------------|
| Free | 10 | 25 | 1 |
| Pro | 60 | 500 | 5 |
| Enterprise | 300 | 5,000 | 20 |

Rate limit headers are included in every response:

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 58
X-RateLimit-Reset: 1710501600
```
