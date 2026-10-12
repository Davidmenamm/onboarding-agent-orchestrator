# Plan — Agent Orchestrator for Digital Onboarding

Status: **v0.3 — APPROVED (implementation started)** · Date: 2026-10-12 · Language: English everywhere (UI, prompts, responses, docs)

---

## Changelog

Newest first. Each version stays **PENDING** until you approve it. Only the changes are listed here, so you can review just the delta.

### v0.3 — 2026-10-12 — APPROVED
- DECIDED Q1: free cloud = **Azure free account** (12-month Postgres Flexible Server + Container Apps free grant). The user creates the account (it needs their identity and card). Phase 12 becomes "Azure free tier", so Stage 2 and Stage 3 merge into one Azure path. Build locally first.
- DECIDED Q2: **Langfuse** kept (compose profile `observability`). **Keycloak dropped.** Local auth = small dev OIDC-style JWT issuer (`/api/v1/auth/dev-login`, roles applicant/analyst/admin, RS256 + JWKS). The API validates JWTs by JWKS URL, so on Azure it points to Entra ID with no code change.
- DECIDED Q3: approvals that pass the guard go **straight through**. No QA sampling for now.
- DECIDED Q4: PII retention for rejected/abandoned cases = **90 days**.

### v0.2 — 2026-10-12 — APPROVED (via v0.3)
**Orchestration**
- CHANGED: the orchestrator went from code-driven routing to an **LLM supervisor**. It decides order, parallelism, retries, remediation and the proposed verdict (D1).
- CHANGED: code now enforces **only hard rules** (thresholds and tags: `confidence < 0.8`, `risk == high`, `verified == false`, missing data, step budget). It can only veto and force human review, never pick an agent (D2, §4).
- ADDED: hard rules are stored as versioned rows in Postgres, not hardcoded (D3).
- ADDED: a Remediation agent (proposes fixes) and an "ask the applicant" interrupt, which makes the conversation two-way (§3, §5.3).
- REMOVED: the deterministic fallback router. When every LLM is down the case goes to a human analyst, so code never takes over routing.

**Data / RAG**
- CHANGED: SQLite + `policies.yaml` → **PostgreSQL 17 + pgvector**, one instance with separate schemas (D7).
- ADDED: RAG pipeline (§6):
  - local FastEmbed embeddings
  - hybrid vector + full-text search merged with RRF
  - reranker
  - citations
  - relevance score used as an uncertainty signal
  - evaluation in CI
- ADDED: structured policy tables (products, required documents) alongside the vectors (D9).

**PII & analytics**
- ADDED: §7:
  - encrypted PII vault and HMAC lookup hash
  - pseudonymization before the LLM, Presidio for free text
  - encrypted checkpoints, log masking, masked UI with an audited reveal
  - least-privilege DB roles, retention purge
- ADDED: event sourcing, CQRS analytics schema, k-anonymity, KPI list, threshold calibration.

**Resilience**
- ADDED: §8:
  - retries at every layer
  - OpenRouter fallback model chain and circuit breaker
  - template fallback for the response
  - idempotency keys, dead-letter queue
  - **fail closed + fail loud**

**Platform / DevOps**
- ADDED: API and worker split with a Postgres queue (procrastinate); `202` + SSE progress (D8).
- ADDED: Nginx load balancer in front of 2 API replicas; Keycloak OIDC roles.
- REMOVED: LangSmith (paid) → ADDED: **Langfuse self-hosted** + Prometheus + Grafana.
- ADDED: CI pipeline, pre-commit, Trivy, gitleaks, Alembic, health checks (§12).
- ADDED: deployment stages local → free cloud → Azure, with a component mapping.
- CHANGED: model = `z-ai/glm-5.3-flashx` (checked on OpenRouter: tool calling supported).

**UI / testing**
- ADDED: full UI spec (applicant, analyst console, admin) and accessibility practices (§11).
- ADDED: test matrix: fake and live LLM, chaos, Playwright + axe, Locust load (§13).

**Docs**
- ADDED: 10 rendered Mermaid diagrams in `docs/diagrams/` (`.mmd` + `.svg` + `.png`).
- CHANGED: everything in English.

### v0.1 — 2026-10-12 — superseded
- Initial draft: hybrid code-driven orchestrator, SQLite, YAML policies, LangSmith.

Diagrams (source `.mmd` + rendered `.svg`/`.png`) live in [`docs/diagrams/`](docs/diagrams/):

| # | Diagram | What it shows |
|---|---------|---------------|
| 01 | [System architecture](docs/diagrams/01-system-architecture.svg) | Nginx LB → API ×2 / workers ×2 → Postgres+pgvector, Keycloak, observability |
| 02 | [Agent graph](docs/diagrams/02-agent-graph.svg) | LLM supervisor, hard-rule guard, sub-agents, interrupts, fallbacks |
| 03 | [Escalation sequence](docs/diagrams/03-sequence-escalation.svg) | End-to-end request with parallel agents, SSE, analyst resume |
| 04 | [PII data flow](docs/diagrams/04-pii-dataflow.svg) | Where PII lives, is encrypted, pseudonymized, redacted |
| 05 | [Onboarding lifecycle](docs/diagrams/05-onboarding-lifecycle.svg) | Status state machine |
| 06 | [RAG pipeline](docs/diagrams/06-rag-pipeline.svg) | Ingestion + hybrid retrieval |
| 07 | [Resilience](docs/diagrams/07-resilience.svg) | Retries/fallbacks per layer, fail closed |
| 08 | [Data model](docs/diagrams/08-data-model.svg) | Tables (ER) |
| 09 | [DevOps & deploy](docs/diagrams/09-devops-deploy.svg) | CI pipeline, local → free cloud → Azure |
| 10 | [UI flows](docs/diagrams/10-ui-flows.svg) | Applicant, analyst, admin screens |

Re-render: `make diagrams` (uses `@mermaid-js/mermaid-cli`).

---

## 1. Goal (from the brief)

`POST /api/v1/onboarding/start` with `{prospect_name, document_id, product}` → an orchestrator coordinates
4 specialised agents → decides **eligible / not eligible / escalate**, and when a check fails or is ambiguous it
**proposes a solution**. Deliverables: architecture diagram, working source (orchestrator, sub-agents, conversational state), git repo.

---

## 2. Core design decisions

| # | Decision | Why |
|---|----------|-----|
| D1 | **The LLM supervisor decides everything that is reasoning:** which agent runs next, ordering, parallelism, retries, remediation strategy, whether to ask the client or an analyst, and the proposed final verdict. | Your choice: the LLM manages uncertainty using confidences and probabilities. |
| D2 | **Code enforces only hard rules**: numeric thresholds and categorical tags (`confidence < 0.8`, `risk_level == "high"`, `verified == false`, a step budget). It never picks an agent and never chooses an order. When a hard rule is violated it can only **veto** a decision and force human review. | Compliance thresholds must be deterministic and auditable. Everything else is left to the LLM (your point 12). |
| D3 | Hard rules are **rows in Postgres** (`policy.hard_rules`: kind = threshold or tag, value, effect, version). They are not hardcoded. | Admins can tune them without a deploy. Versioned for audit. |
| D4 | The guard reads the **raw tool output stored in state**, never the LLM's paraphrase of it. | A hallucinated "confidence 0.9" can't pass. |
| D5 | **Tool allowlist per agent** lives in the orchestrator (`AGENT_TOOLS`). Each sub-agent is built with only its own tools bound. PII arguments are injected from state (`InjectedState`), so the LLM never passes them. | The LLM can't call a tool it was never given. It can't leak or alter IDs either. |
| D6 | Uncertainty is handled with LangGraph **`interrupt()`**: the run pauses for an analyst or for the applicant, and resumes from the checkpoint. | Native human-in-the-loop that survives restarts. |
| D7 | **A single PostgreSQL 17 + pgvector instance** stores app data, checkpoints, vectors, the job queue and analytics, each in its own schema. | One open-source service locally. It maps 1:1 to Azure Database for PostgreSQL Flexible Server, which supports pgvector. |
| D8 | **Stateless API + separate workers.** Graph runs happen in workers fed by a Postgres-backed queue (procrastinate). The API returns `202` and streams progress over SSE. | Horizontal scaling behind a load balancer. LLM latency never blocks API threads. On Azure, KEDA scales the workers on queue depth. |
| D9 | **RAG over a vector DB** for unstructured policy documents. **Structured facts** (required documents per product, hard rules) live in relational tables. | Prose goes to vectors, facts go to tables. Each is queried the way it fits best. |
| D10 | **PII never reaches the external LLM.** OpenRouter only sees pseudonyms. | OpenRouter is the only non-local component. |
| D11 | **Fail closed, fail loud.** Errors never lead to approval. Every fallback is recorded as an event, a metric and a UI badge. | Bank context. |
| D12 | Mocks are **deterministic by `document_id`**. | Every branch can be reproduced in tests and demos. |

---

## 3. Agents and tools

| Agent | Role | Tool allowlist |
|------|------|----------------|
| **Supervisor (orchestrator)** | Reads state and RAG context, then outputs `{next_agents[], rationale, uncertainty, proposed_verdict?}` as structured output | `search_policies` |
| **Identity** | Verifies identity | `verify_identity` |
| **Risk** | Checks risk lists | `check_risk_lists` |
| **Documentation** | Builds the required-document list and justifies it with policy citations | `prepare_documentation`, `search_policies` |
| **Remediation** | Proposes a fix for ambiguity or failure (biometric re-check, extra documents, compliance review…) | `search_policies` |
| **Response** | Writes the applicant-facing message in English, grounded in policy text | `search_policies` |

### Mock behaviour (last digit of `document_id`)

| Last digit | verify_identity | check_risk_lists |
|-----------|------------------|------------------|
| 1 | verified, **0.65** (re-check with extra evidence → 0.91) | low |
| 2 | **not verified**, 0.92 | low |
| 3 | verified, 0.95 | **medium** |
| 4 | verified, 0.95 | **high**, matches `["OFAC: similar name 87%"]` |
| 5 | **timeout** (to test retries and fallback) | low |
| other (e.g. `1712345678`) | verified, 0.95 | low → happy path |

> The API product codes follow the brief (`cuenta_ahorros`, …) because they are identifiers. Their display labels are English ("Savings account").
> `1712345678` isn't a checksum-valid Ecuadorian ID, so validation only checks that it has 10 digits.

---

## 4. The decision table: who decides what

**Hard rules (code, D2):** these are the only rules the code enforces. The values are stored in `policy.hard_rules`.

| Rule | Kind | Condition | Effect |
|------|------|-----------|--------|
| HR1 | threshold | `identity.confidence < 0.8` | Can't approve until an analyst approves |
| HR2 | tag | `risk.risk_level == "high"` | Can't approve until an analyst approves |
| HR3 | tag | `identity.verified == false` (with confidence ≥ 0.8) | Can't approve. Rejection or analyst review only |
| HR4 | presence | the identity or risk result is missing or errored | Can't approve (fail closed) |
| HR5 | threshold | supervisor steps > 15 | Stop the loop and force human review |

**LLM decides (supervisor, D1):** the outcomes below are what the LLM is *expected* to reach, guided by the RAG policy text. They are not hardcoded.

| Identity | Risk | Typical LLM path | Guard check |
|----------|------|-------------------|-------------|
| verified, 0.95 | low | identity ∥ risk → documentation → response: **approved** | passes |
| verified, 0.95 | medium | policy says enhanced due diligence (EDD) → documentation with EDD docs: **approved_edd** | passes |
| verified, 0.65 | any | remediation → ask the applicant for a selfie → re-verify. If still low → analyst | if it tries to approve at 0.65 → **veto** → analyst |
| any | high | remediation → analyst (compliance) | approval blocked until an analyst approves |
| not verified, 0.92 | any | **rejected** with a respectful message, or analyst review | approval blocked |
| tool timeout | — | retry → retry → remediation → analyst | approval blocked (HR4) |

Every veto is recorded as a `guard_override` event. The analyst override rate and the guard override rate are the two numbers that show whether the LLM and the thresholds are well calibrated.

---

## 5. Conversational state management

Four layers:

**5.1 Graph state (per run, passed between steps)**
```python
class OnboardingState(TypedDict):
    onboarding_id: str
    prospect_ref: str                    # UUID → PII vault; no raw PII here
    product: str
    identity: dict | None                # raw tool output (D4)
    risk: dict | None
    documents: list[dict] | None
    policy_context: Annotated[list[dict], operator.add]  # RAG chunks + citations
    supervisor_trail: Annotated[list[dict], operator.add] # each routing decision + rationale + uncertainty
    escalations: Annotated[list[dict], operator.add]
    human_inputs: Annotated[list[dict], operator.add]    # analyst actions, applicant replies
    errors: Annotated[list[dict], operator.add]
    messages: Annotated[list, add_messages]              # applicant conversation (pseudonymized)
    step_count: int
    verdict: Literal["pending","approved","approved_edd","rejected","failed"]
    response: str | None
```
- Each node returns only the keys it changed. **Reducers** (`operator.add`, `add_messages`) merge parallel writes, for example when Identity and Risk append at the same time.

**5.2 Checkpoints (across requests and restarts)**
- `PostgresSaver`, keyed by `thread_id = onboarding_id`, saves a snapshot after every node.
- Checkpoints are encrypted with LangGraph's `EncryptedSerializer` (AES), as defence in depth.
- `interrupt()` pauses the run. `Command(resume=...)` continues it from the exact point, on any worker.
- `get_state_history()` gives a time-travel audit: what the supervisor knew at each decision.

**5.3 Conversation with the applicant**
- `messages` holds the applicant-facing conversation, for example "please upload a selfie" followed by the reply.
- Replies arrive through `POST /onboarding/{id}/messages`. Presidio redacts them, then the run resumes.

**5.4 Sub-agent scratchpad**
- Each sub-agent's internal LLM↔tool loop stays private. Only its result and a summary go back to the parent state.

**Event log (projection of state):** `app.onboarding_events` is append-only. It feeds the UI timeline (SSE via Postgres `LISTEN/NOTIFY`), the audit trail and analytics.

---

## 6. RAG and the vector DB

- **Store:** pgvector in the same Postgres. HNSW index on 384-dim embeddings plus a GIN `tsvector` for full-text search.
- **Embeddings:** FastEmbed `BAAI/bge-small-en-v1.5`. It's local, open source (Apache 2.0) and needs no GPU, so policy text never leaves the machine.
- **Ingestion** (worker job, idempotent):
  1. Load md/pdf/docx.
  2. Chunk by heading (~500 tokens, 50 overlap).
  3. Attach metadata: product, section, version, effective_date, sha256.
  4. Embed, then upsert by hash, so only changed chunks are re-embedded.
- **Retrieval:**
  1. Vector top-20 and full-text top-20.
  2. Merge with Reciprocal Rank Fusion.
  3. Rerank with a cross-encoder to keep the top 5.
  4. Filter by product and effective date.
  5. Apply a minimum relevance score. Below it, the low relevance is reported to the supervisor as an **uncertainty signal**.
- **Citations** travel with every chunk into proposals and the analyst UI.
- **Evaluation:** a golden question set with hit@5 and MRR, gated in CI.
- **Scale path:** the retriever sits behind LangChain's `VectorStore` interface. If the corpus grows to tens of millions of chunks, swap to Azure AI Search or Qdrant through config.
- **Seed corpus:** about 10–15 realistic synthetic documents: product policies (savings, checking, credit card), the KYC/AML procedure, an EDD guide, escalation playbooks and communication guidelines.

---

## 7. PII, anonymization and data analytics

**PII handling (diagram 04)**
- **Classification:** `prospect_name` and `document_id` are direct identifiers. Free text from applicants or analysts may contain PII.
- **At rest:** AES-GCM encrypted columns (`cryptography` library). `document_hash` is an HMAC-SHA256 with a secret pepper and is used for lookups and duplicate checks. Keys come from env locally and from Key Vault on Azure.
- **In the graph:** the state holds only `prospect_ref` plus pseudonyms (`PERSON_1`, `DOC_1`). Tools resolve the real values from the vault internally.
- **LLM boundary:** structured fields are pseudonymized deterministically. Free text goes through **Microsoft Presidio** (MIT) for detection and redaction. Real values are put back only when the final response is rendered.
- **Logs:** a structlog processor masks known PII patterns. Raw state is never logged.
- **UI:** analysts see masked values (`Juan P****`, `******5678`). "Reveal" requires a reason and is audited.
- **Least privilege in the DB:** roles `api_rw`, `worker_rw` and `analytics_ro`. `analytics_ro` can't read `app.prospects`.
- **Retention:** a scheduled job purges PII after `purge_after` (configurable). Events stay pseudonymous.

**Analytics patterns**
- **Event sourcing:** `onboarding_events` is the single source for the timeline, audit and analytics.
- **CQRS read models:** the `analytics` schema holds `fact_onboarding` and `fact_agent_step` (projections of events, with no PII).
- **k-anonymity:** dashboards suppress any group with fewer than 5 cases.
- **KPIs:**
  - straight-through-processing rate
  - escalation rate by reason
  - analyst override rate
  - guard override rate
  - fallback rate
  - p50/p95 time to decision
  - per-agent latency and error rate
  - LLM tokens and cost per onboarding
  - RAG low-relevance rate
- **Threshold calibration:** compare identity confidence with analyst outcomes to tune HR1 using evidence.
- **Scale path:** move data to a warehouse later (Azure Fabric/Synapse) through Postgres logical replication or Debezium.

---

## 8. Resilience: retries, fallbacks, fail closed (diagram 07)

| Layer | Policy | Final fallback |
|------|--------|----------------|
| Client → API | `Idempotency-Key` header with a unique constraint; client retries with backoff on 5xx/429 | Clear error with `correlation_id` |
| Job queue | 3 attempts, exponential backoff with jitter | `failed` status, dead-letter into the analyst queue, alert |
| Tools | LangGraph `RetryPolicy` (3 attempts, backoff 2×, jitter), 10 s timeout | Error written to state; the supervisor decides; HR4 blocks approval |
| LLM | 30 s timeout, 2 retries → OpenRouter fallback chain `glm-5.3-flashx → glm-5.3-flash → glm-4.7-flash` → circuit breaker (5 failures opens it for 60 s) | Run pauses at `interrupt()` and goes to a **human analyst** (code never takes over the routing) |
| Structured output | Pydantic validation, 1 repair retry | Human analyst |
| Response agent | LLM | Approved English **template** for the verdict (only wording, no decision) |
| RAG | pgvector fails → full-text only | Low relevance → uncertainty → human |
| DB | pool pre-ping, retry on transient errors | Checkpoint resumes the run where it died |

**Fail loud:** exceptions are never swallowed. Every fallback produces an event, a Prometheus counter and a UI badge. Alert rules cover the failure rate, an open circuit and queue age.

---

## 9. Stack (all open source except OpenRouter)

| Layer | Choice |
|------|--------|
| Orchestration | LangGraph (`StateGraph`, `Send`, `interrupt`, `RetryPolicy`, `PostgresSaver`) |
| Agents / tools | LangChain (`@tool`, `create_react_agent`, `InjectedState`, structured output) |
| LLM | OpenRouter `z-ai/glm-5.3-flashx` (verified: available, supports tool calling) via `langchain-openai` with `base_url` |
| API | FastAPI, Pydantic v2, pydantic-settings, sse-starlette |
| Queue | procrastinate (Postgres-backed, no Redis) |
| DB | PostgreSQL 17 + pgvector, SQLAlchemy 2 + Alembic migrations |
| Embeddings / rerank | FastEmbed (local ONNX) |
| PII | Presidio (analyzer + anonymizer), `cryptography` |
| Resilience | LangGraph RetryPolicy, tenacity, a small circuit breaker |
| Auth | Dev JWT issuer locally (RS256 + JWKS; roles applicant / analyst / admin) → Entra ID on Azure (same JWKS validation) |
| Load balancer | Nginx (round robin across API replicas, SSE buffering off, rate limit, serves the React build) |
| Observability | Prometheus + Grafana (ops and business dashboards), **Langfuse self-hosted** (MIT) for LLM traces, structlog JSON logs |
| Frontend | React + Vite + TypeScript, shadcn/ui (Radix, accessible) + Tailwind, TanStack Query, React Router, react-hook-form + zod, Recharts |
| Python tooling | uv, ruff, mypy, pytest, testcontainers |
| Testing | pytest, vitest + Testing Library, Playwright + axe, Locust (load) |
| CI/CD | GitHub Actions, GHCR, Trivy, pre-commit, gitleaks |
| IaC (Azure stage) | OpenTofu |

Docker compose profiles keep the default `make up` light: `core` (nginx, api×2, worker×2, postgres) and `observability` (prometheus, grafana, langfuse, which is heavy at ~3–4 GB RAM).

---

## 10. API

| Method | Path | Role | Purpose |
|-------|------|------|---------|
| POST | `/api/v1/onboarding/start` | applicant | Body from the brief → `202 {onboarding_id, status, events_url}`. `?wait=30` blocks until the run finishes or pauses (handy for curl) |
| GET | `/api/v1/onboarding/{id}` | applicant (own) / analyst | State (masked) + timeline |
| GET | `/api/v1/onboarding/{id}/events` | same | SSE live progress |
| POST | `/api/v1/onboarding/{id}/messages` | applicant | Reply to an information request → resume |
| GET | `/api/v1/reviews?status=pending` | analyst | Escalation queue |
| GET | `/api/v1/reviews/{id}` | analyst | Case detail with reasoning trail and citations |
| POST | `/api/v1/reviews/{id}/decision` | analyst | `{action: approve\|reject\|request_info, notes}` → resume |
| POST | `/api/v1/reviews/{id}/reveal` | analyst | `{reason}` → unmasked PII, audited |
| GET | `/api/v1/analytics/summary` | admin | KPIs |
| POST | `/api/v1/admin/knowledge` | admin | Upload a document → ingest job |
| GET/PUT | `/api/v1/admin/hard-rules` | admin | View and edit versioned rules |
| GET | `/healthz`, `/readyz`, `/metrics` | infra | Probes and Prometheus |

---

## 11. UI/UX (diagram 10)

- **Applicant app:**
  - Single-page form with inline validation and a plain-English product picker.
  - **Live progress timeline** (SSE) showing each agent step.
  - Result screens:
    - approved: document checklist and next steps
    - more information needed: a conversational request with upload
    - under review: an ETA
    - rejected: a respectful message with a reason category and a contact
    - error: a correlation id and a retry button
- **Analyst console:**
  - Queue with filters, sorting and SLA badges.
  - Case detail with:
    - confidence bars and risk matches
    - the supervisor's reasoning trail
    - guard vetoes and fallbacks used
    - RAG citations
    - masked PII with an audited reveal
  - Approve, reject or request information, with notes required and a confirmation step.
- **Admin:** analytics dashboard, knowledge base (upload, re-ingest, eval score), hard-rules editor.
- **Practices:**
  - WCAG 2.1 AA: keyboard navigation, focus management, ARIA via Radix, contrast checked with axe in CI.
  - Loading, empty and error states on every view; optimistic but safe actions; toasts.
  - Responsive layout; no PII in URLs.

---

## 12. DevOps (diagram 09)

- **Monorepo:** a `Makefile` (`make up / test / e2e / seed / ingest / diagrams / load`), pre-commit hooks, conventional commits and semver.
- **CI (GitHub Actions):**
  1. lint and typecheck
  2. unit tests
  3. integration tests (Postgres + pgvector service container)
  4. RAG eval
  5. build images and scan them with Trivy
  6. compose up, then Playwright E2E with axe
  7. push to GHCR
- **Runtime:**
  - Health checks on every container.
  - Alembic migrations run as a one-off container before the app starts.
  - Config is 12-factor (env vars only).
  - Graceful shutdown: workers finish or release their jobs.
- **Load balancer:** Nginx in front of 2 API replicas, even at low traffic. It proves the API is stateless, and Locust tests it. Workers scale with `--scale worker=N`.
- **Secrets:** `.env` (gitignored) locally, Key Vault on Azure. gitleaks runs in pre-commit and CI.

**Deployment stages**
1. **Local:** `docker compose up`. Everything is solid and tested here first.
2. **Free cloud:** the same compose on a free VM, with Caddy/Nginx and Let's Encrypt TLS. Choice pending (Q1).
3. **Azure (later):**

| Local component | Azure equivalent |
|-----------------|------------------|
| Nginx | Front Door / App Gateway |
| api + worker containers | Container Apps (KEDA scaling) |
| Postgres + pgvector | Azure Database for PostgreSQL Flexible Server + pgvector |
| Keycloak | Entra ID |
| `.env` | Key Vault + Managed Identity |
| Prometheus / Grafana | Managed Prometheus / Managed Grafana |
| GHCR | ACR |

Provisioned with OpenTofu. No code changes, only config.

---

## 13. Testing (real functionality)

| Level | What | Tool |
|------|------|------|
| Unit | hard-rule guard (every rule × boundary values 0.79/0.8), pseudonymizer, encryption, chunker, RRF | pytest |
| Graph scenarios | every row of §4 with a scripted fake LLM (deterministic), including veto and fallback paths | pytest |
| Live LLM | the same scenarios against real glm-5.3-flashx, asserting outcomes rather than wording (marked, run on demand) | pytest `-m live` |
| Integration | API + Postgres + queue + checkpoint resume after killing a worker | testcontainers |
| RAG eval | hit@5 and MRR on a golden set, with a CI threshold | pytest |
| Chaos | LLM down, tool timeout, pgvector down, worker killed mid-run: never approved, always visible | pytest + compose |
| Frontend | components and forms | vitest + Testing Library |
| E2E | the 6 mock scenarios in a real browser, including analyst approve/reject and the applicant reply | Playwright + axe |
| Load | 50 concurrent onboardings through Nginx, no 5xx, p95 budget | Locust |

---

## 14. Repo layout

```
backend/
  app/
    main.py            # FastAPI app, routers
    api/               # onboarding, reviews, admin, analytics routes
    graph/             # state.py, supervisor.py, guard.py, agents.py, build.py
    tools/             # mocks.py, search_policies.py
    rag/               # ingest.py, retriever.py
    pii/               # vault.py, pseudonymize.py, presidio.py
    db/                # models.py, migrations/ (alembic)
    worker.py          # procrastinate tasks
    resilience.py      # LLM factory with fallbacks + circuit breaker
    settings.py
  tests/
  pyproject.toml
frontend/              # Vite React TS app
knowledge/             # seed policy corpus (md)
infra/
  nginx/  keycloak/  grafana/  prometheus/
  azure/ (later, OpenTofu)
docs/
  diagrams/            # .mmd + .svg + .png
  adr/                 # architecture decision records (D1–D12)
docker-compose.yml
Makefile
.env.example
README.md
```

---

## 15. Phases

| # | Phase | Done when |
|---|-------|-----------|
| 0 | Foundations: git, uv, compose (postgres+pgvector), Makefile, pre-commit, CI skeleton, Alembic | `make up && make test` green |
| 1 | Data layer: schemas, PII vault, events, policy tables and hard-rule seed | unit tests for encryption, hashing and rules |
| 2 | Mock tools + hard-rule guard | boundary tests pass |
| 3 | RAG: corpus, ingestion, hybrid retrieval, eval | hit@5 ≥ target |
| 4 | Graph: LLM supervisor + sub-agents + allowlists + pseudonymization (fake LLM first, then glm-5.3-flashx) | §4 scenarios pass with both fake and live LLMs |
| 5 | Resilience: retries, fallback chain, circuit breaker, templates | chaos tests pass |
| 6 | HITL + applicant replies, workers, SSE | kill-worker-and-resume test passes |
| 7 | API + Keycloak auth + idempotency + rate limit | API integration tests pass |
| 8 | UI: applicant, analyst, admin | Playwright scenarios + axe pass |
| 9 | Observability + analytics: Prometheus, Grafana dashboards, Langfuse, alerts | dashboards show live KPIs |
| 10 | Nginx LB + Locust load test | 2 replicas, no 5xx under load |
| 11 | Docs: README, ADRs, runbook, rendered diagrams | a newcomer can run it in 10 minutes |
| 12 | Free cloud deploy | public URL with TLS |
| 13 | Azure (later) | OpenTofu apply |

---

## 16. Open questions

- **Q1 Free cloud:**
  - Recommended: **Oracle Cloud Always Free** (ARM VM, 4 cores and 24 GB RAM, no expiry). It runs this exact compose, including Langfuse and Keycloak.
  - Alternative: an **Azure free account** (12-month free Postgres Flexible Server + Container Apps free grant). It's closer to the final target, but it expires and the free grant is too small for the whole stack.
  - Free-tier limits change, so verify them at deploy time.
- **Q2** Keycloak and Langfuse add weight but are the "real" choices. OK, or do you want a lighter auth stub locally?
- **Q3** Should the supervisor's proposed verdict be final when the guard passes (straight-through approval), or should every approval also be sampled for analyst QA (for example 10%)?
- **Q4** How long should PII be kept for rejected or abandoned cases (default 90 days)?
