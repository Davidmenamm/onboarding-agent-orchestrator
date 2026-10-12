"""Job queue schema (procrastinate), applied once."""
from alembic import op
from procrastinate.schema import SchemaManager
from sqlalchemy import text

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    if op.get_bind().execute(text("SELECT to_regclass('public.procrastinate_jobs')")).scalar() is None:
        op.execute(text(SchemaManager.get_schema().replace("%", "%%")))


def downgrade() -> None:
    pass
