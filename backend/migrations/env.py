# ponytail: migrations are raw SQL via op.execute; no ORM models to autogenerate from.
from alembic import context
from sqlalchemy import create_engine

from app.settings import settings

url = settings().database_url.replace("postgresql://", "postgresql+psycopg://", 1)
with create_engine(url).connect() as conn:
    context.configure(connection=conn, version_table_schema="public")
    with context.begin_transaction():
        context.run_migrations()
