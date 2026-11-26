"""initial schema (v0.1)

The initial revision materialises the SQLAlchemy metadata so the ORM models and
the database can never drift on a fresh install.  Subsequent revisions are
explicit and additive.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-25
"""

from __future__ import annotations

from alembic import op

from family_cfo.database.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
