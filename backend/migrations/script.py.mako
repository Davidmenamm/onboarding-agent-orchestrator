"""${message}"""
from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
