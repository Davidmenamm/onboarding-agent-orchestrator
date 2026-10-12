"""Use-cases shared by API and worker."""
from langgraph.types import Command

from app import db, events, pii, rag


class InvalidProduct(ValueError):
    pass


def create_onboarding(name: str, document_id: str, product: str, idempotency_key: str | None, owner: str | None) -> tuple[str, bool]:
    """Returns (onboarding_id, created). Same idempotency key -> same onboarding, nothing duplicated."""
    if idempotency_key:
        row = db.fetch_one("SELECT id FROM app.onboardings WHERE idempotency_key = %s", idempotency_key)
        if row:
            return str(row["id"]), False
    if product not in {p["code"] for p in rag.products()}:
        raise InvalidProduct(product)
    prospect = pii.store_prospect(name, document_id)
    row = db.fetch_one(
        "INSERT INTO app.onboardings (prospect_id, product_code, idempotency_key, owner_sub) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        prospect, product, idempotency_key, owner,
    )
    if row is None:  # lost a race on the same key
        db.execute("DELETE FROM app.prospects WHERE id = %s", prospect)
        return create_onboarding(name, document_id, product, idempotency_key, owner)
    oid = str(row["id"])
    events.emit(oid, "received", "applicant", product=product)
    return oid, True


def run(onboarding_id: str, resume: dict | None = None, retrying: bool = False) -> dict:
    """Run (or resume) the graph until it finishes or pauses at an interrupt. Called by the worker.
    On a job retry we continue from the last checkpoint instead of starting over."""
    from app.graph.build import graph

    cfg = {"configurable": {"thread_id": onboarding_id}}
    if resume is not None:
        return graph().invoke(Command(resume=resume), cfg)
    if retrying and graph().get_state(cfg).next:
        return graph().invoke(None, cfg)
    row = db.fetch_one("SELECT prospect_id, product_code FROM app.onboardings WHERE id = %s", onboarding_id)
    return graph().invoke(
        {"onboarding_id": onboarding_id, "prospect_ref": str(row["prospect_id"]), "product": row["product_code"]}, cfg  # type: ignore[index]
    )


def graph_state(onboarding_id: str) -> dict:
    from app.graph.build import graph

    snap = graph().get_state({"configurable": {"thread_id": onboarding_id}})
    return snap.values or {}
