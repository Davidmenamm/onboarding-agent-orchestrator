"""Append-only event log (pseudonymous payloads only). Feeds SSE timeline, audit and analytics."""
import json

import structlog

from app import db

log = structlog.get_logger()


def emit(onboarding_id: str, type_: str, actor: str, **payload) -> None:
    log.info(type_, onboarding_id=onboarding_id, actor=actor, **{k: v for k, v in payload.items() if k != "proposal"})
    db.execute(
        "INSERT INTO app.onboarding_events (onboarding_id, type, actor, payload) VALUES (%s,%s,%s,%s::jsonb)",
        onboarding_id,
        type_,
        actor,
        json.dumps(payload, default=str),
    )


def set_status(onboarding_id: str, status: str, **cols) -> None:
    sets = ", ".join(f"{k} = %s" for k in cols)
    db.execute(
        f"UPDATE app.onboardings SET status = %s, updated_at = now(){', ' + sets if sets else ''} WHERE id = %s",
        status,
        *cols.values(),
        onboarding_id,
    )
    emit(onboarding_id, "status_changed", "system", status=status)
