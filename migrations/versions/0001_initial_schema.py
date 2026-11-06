"""Initial schema: tickets (queue) and kb_articles (trigram search).

Revision ID: 0001
Revises:
Create Date: 2026-11-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATUSES = (
    "'new', 'generating', 'draft', 'approved', 'sending', 'sent', "
    "'rejected', 'generation_failed', 'send_failed'"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "kb_articles",
        sa.Column("slug", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
    )
    # Must match the expression in app/kb.py so `<%` can use the index.
    op.execute(
        "CREATE INDEX kb_articles_trgm_idx ON kb_articles "
        "USING gin ((title || ' ' || body) gin_trgm_ops)"
    )

    op.create_table(
        "tickets",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("event_id", sa.Text(), nullable=False, unique=True),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("customer_email", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("reply_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="new", nullable=False),
        sa.Column("draft", sa.Text()),
        sa.Column("citations", postgresql.JSONB()),
        sa.Column("final_message", sa.Text()),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="tickets_status_check"),
    )
    # Supports the worker claim: WHERE status = ? AND next_attempt_at <= now().
    op.create_index("tickets_status_next_attempt_at_idx", "tickets", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("tickets")
    op.drop_table("kb_articles")
