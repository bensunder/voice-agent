-- Campaign orchestration (MAF workflows), token-cost ledger and plan cache.

ALTER TABLE call_attempt DROP CONSTRAINT call_attempt_channel_check;
ALTER TABLE call_attempt ADD CONSTRAINT call_attempt_channel_check
    CHECK (channel IN ('teams_phone', 'browser', 'simulation'));
ALTER TABLE call_attempt ADD COLUMN campaign_id uuid;
ALTER TABLE call_attempt ADD COLUMN sim_complete_at timestamptz;
CREATE INDEX call_attempt_sim_due_idx ON call_attempt (sim_complete_at)
    WHERE channel = 'simulation' AND status = 'in_progress';

CREATE TABLE campaign (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_key             text        NOT NULL,
    name                   text        NOT NULL,
    mode                   text        NOT NULL CHECK (mode IN ('live', 'simulation')),
    status                 text        NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft','running','paused','completed','stopped')),
    max_concurrent         integer     NOT NULL CHECK (max_concurrent BETWEEN 1 AND 500),
    max_attempts           integer     NOT NULL CHECK (max_attempts BETWEEN 1 AND 6),
    retry_seconds          integer[]   NOT NULL,
    escalation_daily_cap   integer     NOT NULL CHECK (escalation_daily_cap >= 0),
    llm_budget_usd         numeric(12,4) NOT NULL CHECK (llm_budget_usd >= 0),
    respect_calling_window boolean     NOT NULL DEFAULT true,
    created_at             timestamptz NOT NULL DEFAULT now(),
    started_at             timestamptz,
    ended_at               timestamptz
);

CREATE TABLE campaign_lead (
    campaign_id     uuid        NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    lead_id         uuid        NOT NULL REFERENCES lead(id) ON DELETE CASCADE,
    status          text        NOT NULL DEFAULT 'pending' CHECK (status IN (
                        'pending','dialing','in_call','retry_wait','escalated','nurture',
                        'disqualified','suppressed','exhausted','closed')),
    attempts        integer     NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_attempt_id uuid,
    last_outcome    text,
    last_reason     text,
    priority        integer     NOT NULL DEFAULT 0,
    plan            jsonb,
    plan_source     text,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, lead_id)
);
CREATE INDEX campaign_lead_due_idx ON campaign_lead (campaign_id, status, priority DESC, next_attempt_at);
CREATE INDEX campaign_lead_attempt_idx ON campaign_lead (last_attempt_id);

-- Every model call, priced at the time it was made (source of truth for cost management).
CREATE TABLE llm_usage (
    id             bigserial PRIMARY KEY,
    campaign_id    uuid,
    attempt_id     uuid,
    agent          text        NOT NULL,
    model          text        NOT NULL,
    input_tokens   integer     NOT NULL DEFAULT 0,
    output_tokens  integer     NOT NULL DEFAULT 0,
    cached_tokens  integer     NOT NULL DEFAULT 0,
    cost_usd       numeric(12,6) NOT NULL DEFAULT 0,
    latency_ms     integer     NOT NULL DEFAULT 0,
    status         text        NOT NULL CHECK (status IN ('reserved','ok','cache_hit','guardrail_fallback','budget_skip','error')),
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_usage_campaign_idx ON llm_usage (campaign_id, created_at);
CREATE INDEX llm_usage_day_idx ON llm_usage (created_at);

-- Identical grounded fact packets reuse the same validated plan (saves tokens at scale).
CREATE TABLE plan_cache (
    cache_key   text PRIMARY KEY,
    plan        jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE guardrail_event (
    id           bigserial PRIMARY KEY,
    campaign_id  uuid,
    attempt_id   uuid,
    stage        text NOT NULL CHECK (stage IN ('input','output','action','budget')),
    rule         text NOT NULL,
    detail       text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX guardrail_event_campaign_idx ON guardrail_event (campaign_id, created_at);
