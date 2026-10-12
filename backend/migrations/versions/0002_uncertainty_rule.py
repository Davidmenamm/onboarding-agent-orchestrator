"""HR6: supervisor self-reported uncertainty threshold + idempotent review ids."""
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.execute("""
    INSERT INTO policy.hard_rules (code, kind, field, operator, value, effect, description) VALUES
     ('HR6_supervisor_uncertainty','threshold','supervisor_uncertainty','>','0.7','require_human_review',
      'If the supervisor reports uncertainty above 0.7 its verdict needs analyst review.');
    """)


def downgrade() -> None:
    op.execute("DELETE FROM policy.hard_rules WHERE code = 'HR6_supervisor_uncertainty'")
