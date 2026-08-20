"""dashboard: scoped suppressions, persisted dns records, provisioning jobs

Revision ID: 16ef38ef951f
Revises: f53f35ee9839
Create Date: 2026-08-20 22:01:12.304419+00:00

Three changes, all additive:

1. `suppressions.project_id` -- records which project's sending produced an
   entry. Fixes a cross-tenant disclosure: GET /v1/suppressions returned every
   suppressed address on the installation, letting one product enumerate
   another product's bounced customers. Enforcement stays account-wide.

2. `domains.required_records` -- the DNS records to publish, captured at
   add/verify time. Lets the dashboard display them, which it otherwise cannot:
   fetching them live needs the MXRoute account-root credential and role=api
   holds none.

3. `provisioning_jobs` -- a request queue so the dashboard can ask for a
   privileged action without holding the credential to perform it.

Data impact:
  None. Every column is nullable with no backfill, and the new table starts
  empty. Existing suppressions keep project_id NULL, which means "operator-only
  visibility" -- the conservative reading, since we cannot retroactively
  determine which project caused an entry that predates the column.

Rollback:
  Safe. `downgrade()` drops only what this revision added. The one thing it
  destroys is the project attribution on suppressions, which is metadata --
  no address is un-suppressed and no mail behaviour changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "16ef38ef951f"
down_revision: str | None = "f53f35ee9839"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provisioning_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "job_type",
            sa.Enum("add_domain", "verify_domain", name="job_type"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "succeeded", "failed", name="job_status"),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "requested_by",
            sa.String(length=40),
            server_default=sa.text("'dashboard'"),
            nullable=False,
        ),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_jobs_claimable", "provisioning_jobs", ["status", "created_at"], unique=False
    )

    op.add_column(
        "domains",
        sa.Column("required_records", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.add_column("suppressions", sa.Column("project_id", sa.Integer(), nullable=True))
    op.create_index("ix_suppressions_project", "suppressions", ["project_id"], unique=False)
    op.create_foreign_key(
        # Named, so downgrade can drop it. Autogenerate emitted None here,
        # which produces a server-generated name that the downgrade cannot
        # reference -- the rollback would have failed on the first statement.
        "fk_suppressions_project",
        "suppressions",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_suppressions_project", "suppressions", type_="foreignkey")
    op.drop_index("ix_suppressions_project", table_name="suppressions")
    op.drop_column("suppressions", "project_id")

    op.drop_column("domains", "required_records")

    op.drop_index("ix_jobs_claimable", table_name="provisioning_jobs")
    op.drop_table("provisioning_jobs")

    # Postgres does NOT drop an enum type with the table that used it.
    # Autogenerate omits this, so a downgrade followed by an upgrade would fail
    # on "type job_type already exists" -- which is exactly the moment someone
    # is rolling back under pressure and least wants a surprise.
    sa.Enum(name="job_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="job_status").drop(op.get_bind(), checkfirst=True)
