-- AI Sales Agent: system of record for leads, calls, qualification and outbox.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE lead (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_key       text        NOT NULL,
    first_name       text        NOT NULL,
    last_name        text        NOT NULL DEFAULT '',
    company          text        NOT NULL,
    phone_e164       text        NOT NULL,
    email            text,
    timezone         text        NOT NULL,
    product_interest text        NOT NULL,
    consent_source   text        NOT NULL,
    consent_text     text        NOT NULL,
    consent_at       timestamptz NOT NULL,
    status           text        NOT NULL DEFAULT 'new',
    owner_upn        text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_key, phone_e164)
);

CREATE TABLE opt_out (
    tenant_key  text        NOT NULL,
    phone_e164  text        NOT NULL,
    source      text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_key, phone_e164)
);

CREATE TABLE call_attempt (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id             uuid        NOT NULL REFERENCES lead(id) ON DELETE CASCADE,
    channel             text        NOT NULL CHECK (channel IN ('teams_phone', 'browser')),
    status              text        NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','blocked','dialing','in_progress','completed','failed','cancelled')),
    gate_reason         text,
    idempotency_key     text        NOT NULL UNIQUE,
    foundry_call_job_id text,
    foundry_status      text,
    terminal_reason     text,
    outcome             text,
    summary             text,
    next_action         text,
    armed_until         timestamptz,
    started_at          timestamptz,
    ended_at            timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX call_attempt_lead_idx ON call_attempt (lead_id, created_at DESC);
CREATE INDEX call_attempt_active_job_idx ON call_attempt (status) WHERE foundry_call_job_id IS NOT NULL;
-- At most one armed browser session at a time.
CREATE UNIQUE INDEX call_attempt_one_armed_browser ON call_attempt ((true))
    WHERE channel = 'browser' AND status IN ('queued','in_progress');

CREATE TABLE qualification_slot (
    attempt_id  uuid        NOT NULL REFERENCES call_attempt(id) ON DELETE CASCADE,
    name        text        NOT NULL,
    value       jsonb       NOT NULL,
    confidence  real        NOT NULL,
    evidence    text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (attempt_id, name)
);

CREATE TABLE score_result (
    attempt_id         uuid PRIMARY KEY REFERENCES call_attempt(id) ON DELETE CASCADE,
    score              integer     NOT NULL,
    band               text        NOT NULL,
    reason_codes       text[]      NOT NULL,
    missing            text[]      NOT NULL,
    verify             text[]      NOT NULL,
    rubric_version     text        NOT NULL,
    valuation          jsonb,
    recommended_action text        NOT NULL,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE slot_hold (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id  uuid        NOT NULL REFERENCES call_attempt(id) ON DELETE CASCADE,
    rep_upn     text        NOT NULL,
    slot_start  timestamptz NOT NULL,
    slot_end    timestamptz NOT NULL,
    status      text        NOT NULL CHECK (status IN ('held','booked','released','expired')),
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- The concurrency guarantee: one live hold/booking per rep per start time.
CREATE UNIQUE INDEX slot_hold_live_uniq ON slot_hold (rep_upn, slot_start)
    WHERE status IN ('held','booked');

CREATE TABLE meeting (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id      uuid        NOT NULL REFERENCES call_attempt(id) ON DELETE CASCADE,
    lead_id         uuid        NOT NULL REFERENCES lead(id) ON DELETE CASCADE,
    rep_upn         text        NOT NULL,
    starts_at       timestamptz NOT NULL,
    ends_at         timestamptz NOT NULL,
    graph_event_id  text,
    join_url        text,
    status          text        NOT NULL CHECK (status IN ('pending_graph','booked','failed')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (attempt_id)
);

CREATE TABLE outbox (
    id              bigserial PRIMARY KEY,
    topic           text        NOT NULL,
    dedupe_key      text        NOT NULL UNIQUE,
    payload         jsonb       NOT NULL,
    status          text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','dead')),
    attempts        integer     NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_due_idx ON outbox (next_attempt_at) WHERE status = 'pending';

CREATE TABLE audit_event (
    id          bigserial PRIMARY KEY,
    lead_id     uuid,
    attempt_id  uuid,
    kind        text        NOT NULL,
    detail      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_event_attempt_idx ON audit_event (attempt_id, id);

-- Append-only audit log.
CREATE FUNCTION audit_event_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only';
END $$;
CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_immutable();

-- Live event stream for the cockpit (small payload; consumers load the row by id).
CREATE FUNCTION audit_event_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('sales_events', json_build_object(
        'id', NEW.id, 'kind', NEW.kind,
        'attempt_id', NEW.attempt_id, 'lead_id', NEW.lead_id)::text);
    RETURN NEW;
END $$;
CREATE TRIGGER audit_event_notify AFTER INSERT ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_notify();
