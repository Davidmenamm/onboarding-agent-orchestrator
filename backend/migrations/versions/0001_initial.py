"""Initial schemas: app, policy, rag, analytics."""
from alembic import op

revision = "0001"
down_revision = None

UP = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS policy;
CREATE SCHEMA IF NOT EXISTS rag;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS langgraph;

-- ---------- policy (guard config only; business policies live in rag.*, the single source of truth) ----------
-- Hard rules = ONLY thresholds / tags / presence checks. Never routing.
CREATE TABLE policy.hard_rules (
    code        text PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('threshold','tag','presence')),
    field       text NOT NULL,              -- dotted path in graph state, e.g. identity.confidence
    operator    text NOT NULL CHECK (operator IN ('<','<=','>','>=','==','!=','missing')),
    value       jsonb,
    effect      text NOT NULL CHECK (effect IN ('block_approval','require_human_review','stop_loop')),
    description text NOT NULL,
    version     int  NOT NULL DEFAULT 1,
    active      boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------- app ----------
CREATE TABLE app.prospects (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name_enc        bytea NOT NULL,               -- AES-GCM
    document_id_enc bytea NOT NULL,               -- AES-GCM
    document_hash   text  NOT NULL,               -- HMAC-SHA256, lookup/dedup without decrypting
    created_at      timestamptz NOT NULL DEFAULT now(),
    purge_after     timestamptz                   -- set when case ends rejected/abandoned
);
CREATE INDEX ON app.prospects (document_hash);

CREATE TABLE app.onboardings (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),   -- = LangGraph thread_id
    prospect_id     uuid NOT NULL REFERENCES app.prospects(id) ON DELETE CASCADE,
    product_code    text NOT NULL,                -- validated against rag.documents (policy corpus)
    status          text NOT NULL DEFAULT 'received' CHECK (status IN
                    ('received','processing','awaiting_analyst','awaiting_client',
                     'approved','approved_edd','rejected','failed','expired')),
    verdict         text,
    response        text,
    idempotency_key text UNIQUE,
    owner_sub       text,                         -- JWT subject of the applicant
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON app.onboardings (status);

-- Append-only event log: timeline UI, SSE, audit, analytics. Pseudonymous payloads only.
CREATE TABLE app.onboarding_events (
    id             bigserial PRIMARY KEY,
    onboarding_id  uuid NOT NULL REFERENCES app.onboardings(id) ON DELETE CASCADE,
    type           text NOT NULL,
    payload        jsonb NOT NULL DEFAULT '{}',
    actor          text NOT NULL,
    correlation_id text,
    ts             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON app.onboarding_events (onboarding_id, id);

CREATE TABLE app.reviews (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    onboarding_id uuid NOT NULL REFERENCES app.onboardings(id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('analyst','client')),
    reasons       text[] NOT NULL DEFAULT '{}',
    proposal      jsonb NOT NULL DEFAULT '{}',
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','resolved')),
    action        text,
    notes         text,
    resolved_by   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    resolved_at   timestamptz
);
CREATE INDEX ON app.reviews (status, kind);

CREATE TABLE app.pii_reveals (
    id            bigserial PRIMARY KEY,
    onboarding_id uuid NOT NULL REFERENCES app.onboardings(id) ON DELETE CASCADE,
    actor         text NOT NULL,
    reason        text NOT NULL,
    ts            timestamptz NOT NULL DEFAULT now()
);

-- Notify SSE listeners on every event.
CREATE FUNCTION app.notify_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('onboarding_events', NEW.onboarding_id::text || ':' || NEW.id);
  RETURN NEW;
END $$;
CREATE TRIGGER onboarding_events_notify AFTER INSERT ON app.onboarding_events
  FOR EACH ROW EXECUTE FUNCTION app.notify_event();

-- ---------- rag ----------
CREATE TABLE rag.documents (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_path    text UNIQUE NOT NULL,
    title          text NOT NULL,
    product_code   text,                 -- null = applies to all products
    doc_type       text NOT NULL,        -- product_policy | procedure | playbook | guideline
    metadata       jsonb NOT NULL DEFAULT '{}',  -- front-matter: label, required_documents, edd_documents...
    sha256         text NOT NULL,
    effective_date date,
    ingested_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rag.chunks (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id    uuid NOT NULL REFERENCES rag.documents(id) ON DELETE CASCADE,
    chunk_index    int  NOT NULL,
    section        text,
    content        text NOT NULL,
    metadata       jsonb NOT NULL DEFAULT '{}',
    embedding      vector(384) NOT NULL,
    fts            tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(section,'') || ' ' || content)) STORED
);
CREATE INDEX ON rag.chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON rag.chunks USING gin (fts);
CREATE INDEX ON rag.chunks (document_id);

-- ---------- analytics (no PII; read model over events) ----------
CREATE VIEW analytics.fact_onboarding AS
SELECT o.id AS onboarding_id,
       o.product_code,
       o.status,
       o.verdict,
       o.created_at,
       date_trunc('day', o.created_at)::date AS day,
       EXTRACT(EPOCH FROM (o.updated_at - o.created_at)) * 1000 AS duration_ms,
       EXISTS (SELECT 1 FROM app.reviews r WHERE r.onboarding_id = o.id AND r.kind = 'analyst') AS escalated,
       (SELECT array_agg(DISTINCT x) FROM app.reviews r, unnest(r.reasons) x WHERE r.onboarding_id = o.id) AS escalation_reasons,
       EXISTS (SELECT 1 FROM app.onboarding_events e WHERE e.onboarding_id = o.id AND e.type = 'guard_override') AS guard_override,
       EXISTS (SELECT 1 FROM app.onboarding_events e WHERE e.onboarding_id = o.id AND e.type = 'fallback_used') AS fallback_used,
       (SELECT coalesce(sum((e.payload->>'cost_usd')::numeric), 0) FROM app.onboarding_events e
         WHERE e.onboarding_id = o.id AND e.type = 'llm_usage') AS llm_cost_usd
FROM app.onboardings o;

CREATE VIEW analytics.fact_agent_step AS
SELECT e.onboarding_id, e.payload->>'agent' AS agent, (e.payload->>'duration_ms')::numeric AS duration_ms,
       (e.payload->>'ok')::boolean AS ok, e.ts
FROM app.onboarding_events e WHERE e.type = 'agent_finished';

-- Least privilege: analytics role sees only the analytics schema.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_ro') THEN
    CREATE ROLE analytics_ro NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA analytics TO analytics_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO analytics_ro;
"""

SEED = """
INSERT INTO policy.hard_rules (code, kind, field, operator, value, effect, description) VALUES
 ('HR1_min_identity_confidence','threshold','identity.confidence','<','0.8','require_human_review',
  'Identity confidence below 0.8 cannot be approved without analyst review.'),
 ('HR2_high_risk','tag','risk.risk_level','==','"high"','require_human_review',
  'High risk level cannot be approved without analyst review.'),
 ('HR3_identity_not_verified','tag','identity.verified','==','false','block_approval',
  'An unverified identity can never be approved.'),
 ('HR4a_identity_missing','presence','identity','missing',null,'block_approval',
  'No approval without an identity result.'),
 ('HR4b_risk_missing','presence','risk','missing',null,'block_approval',
  'No approval without a risk result.'),
 ('HR5_step_budget','threshold','step_count','>','15','stop_loop',
  'Supervisor loop exceeding 15 steps is stopped and sent to human review.');
"""


def upgrade() -> None:
    op.execute(UP)
    op.execute(SEED)


def downgrade() -> None:
    op.execute("DROP SCHEMA app, policy, rag, analytics, langgraph CASCADE")
