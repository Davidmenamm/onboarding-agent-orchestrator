"""Background worker: runs/resumes graphs from a Postgres-backed queue (procrastinate, no Redis).
Scale with `docker compose up --scale worker=N`; jobs are taken with SKIP LOCKED."""
import procrastinate
import structlog

from app import db, events, service
from app.logging import setup_logging
from app.settings import settings

setup_logging()
log = structlog.get_logger()
MAX_ATTEMPTS = 3

queue = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=settings().database_url))


@queue.task(name="run_onboarding", pass_context=True, retry=procrastinate.RetryStrategy(max_attempts=MAX_ATTEMPTS - 1, exponential_wait=2))
def run_onboarding(context, onboarding_id: str, resume: dict | None = None) -> None:
    attempt = context.job.attempts + 1
    try:
        service.run(onboarding_id, resume, retrying=attempt > 1)
    except Exception as e:
        log.exception("job.failed", onboarding_id=onboarding_id, attempt=attempt)
        events.emit(onboarding_id, "job_failed", "system", attempt=attempt, error=f"{type(e).__name__}: {e}"[:300])
        if attempt >= MAX_ATTEMPTS:
            # Fail closed + loud: never approved; dead-letter into the analyst queue.
            events.set_status(onboarding_id, "failed")
            db.execute(
                "INSERT INTO app.reviews (onboarding_id, kind, reasons, proposal) VALUES (%s,'analyst',%s,%s::jsonb)",
                onboarding_id, ["system_failure"],
                events.json.dumps({"target": "analyst", "reasons": ["system_failure"], "summary": f"Processing failed after {attempt} attempts: {type(e).__name__}", "recommended_actions": ["Check logs with the onboarding id", "Retry or decide manually"], "citations": []}),
            )
        raise


def enqueue(onboarding_id: str, resume: dict | None = None) -> None:
    with queue.open():
        run_onboarding.defer(onboarding_id=onboarding_id, resume=resume)
