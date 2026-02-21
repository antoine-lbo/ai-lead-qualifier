-- ============================================
-- AI Lead Qualifier — Database Initialization
-- ============================================
-- Run via docker-compose (auto-mounted in postgres container)
-- or manually: psql -U qualifier -d lead_qualifier -f init-db.sql

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

CREATE TYPE lead_tier AS ENUM ('hot', 'warm', 'cold');
CREATE TYPE lead_status AS ENUM ('new', 'qualified', 'routed', 'converted', 'disqualified');
CREATE TYPE enrichment_source AS ENUM ('clearbit', 'linkedin', 'manual', 'api');

-- ============================================
-- LEADS
-- ============================================

CREATE TABLE leads (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  email TEXT NOT NULL,
  company TEXT,
  full_name TEXT,
  message TEXT,
  source TEXT,
  score INTEGER CHECK (score >= 0 AND score <= 100),
  tier lead_tier DEFAULT 'cold',
  status lead_status DEFAULT 'new',
  reasoning TEXT,
  recommended_action TEXT,
  processing_time_ms INTEGER,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  qualified_at TIMESTAMPTZ,
  routed_at TIMESTAMPTZ
);

-- ============================================
-- ENRICHMENT DATA
-- ============================================

CREATE TABLE enrichment_data (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  source enrichment_source NOT NULL DEFAULT 'api',
  company_size TEXT,
  industry TEXT,
  estimated_revenue TEXT,
  location TEXT,
  website TEXT,
  linkedin_url TEXT,
  technologies JSONB DEFAULT $$[]$$::jsonb,
  raw_data JSONB DEFAULT $${}$$::jsonb,
  fetched_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- ROUTING HISTORY
-- ============================================

CREATE TABLE routing_history (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  assigned_to TEXT NOT NULL,
  reason TEXT,
  slack_notified BOOLEAN DEFAULT FALSE,
  crm_synced BOOLEAN DEFAULT FALSE,
  crm_record_id TEXT,
  routed_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- SCORING AUDIT LOG
-- ============================================

CREATE TABLE scoring_audit (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  model_version TEXT NOT NULL,
  company_fit_score NUMERIC(5,2),
  intent_signal_score NUMERIC(5,2),
  budget_indicator_score NUMERIC(5,2),
  urgency_score NUMERIC(5,2),
  final_score INTEGER NOT NULL,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  llm_latency_ms INTEGER,
  raw_response JSONB,
  scored_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- API KEYS
-- ============================================

CREATE TABLE api_keys (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name TEXT NOT NULL,
  key_hash TEXT UNIQUE NOT NULL,
  prefix TEXT NOT NULL,
  is_active BOOLEAN DEFAULT TRUE,
  rate_limit INTEGER DEFAULT 100,
  last_used_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ
);

-- ============================================
-- INDEXES
-- ============================================

CREATE INDEX idx_leads_email ON leads(email);
CREATE INDEX idx_leads_company ON leads USING gin(company gin_trgm_ops);
CREATE INDEX idx_leads_tier ON leads(tier);
CREATE INDEX idx_leads_status ON leads(status);
CREATE INDEX idx_leads_score ON leads(score DESC);
CREATE INDEX idx_leads_created ON leads(created_at DESC);
CREATE INDEX idx_enrichment_lead ON enrichment_data(lead_id);
CREATE INDEX idx_routing_lead ON routing_history(lead_id);
CREATE INDEX idx_scoring_lead ON scoring_audit(lead_id);
CREATE INDEX idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX idx_api_keys_prefix ON api_keys(prefix);

-- ============================================
-- FUNCTIONS
-- ============================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_leads_updated_at
  BEFORE UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Daily stats materialized view
CREATE MATERIALIZED VIEW daily_lead_stats AS
SELECT
  DATE_TRUNC('day', created_at) AS day,
  COUNT(*) AS total_leads,
  COUNT(*) FILTER (WHERE tier = 'hot') AS hot_leads,
  COUNT(*) FILTER (WHERE tier = 'warm') AS warm_leads,
  COUNT(*) FILTER (WHERE tier = 'cold') AS cold_leads,
  AVG(score) AS avg_score,
  AVG(processing_time_ms) AS avg_processing_time,
  COUNT(*) FILTER (WHERE status = 'converted') AS conversions
FROM leads
GROUP BY 1
ORDER BY 1 DESC;

CREATE UNIQUE INDEX idx_daily_stats_day ON daily_lead_stats(day);
