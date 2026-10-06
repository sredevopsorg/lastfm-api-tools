"""Add album overview and wiki_published columns.

Album getInfo **does** return a wiki, contrary to what the phase 3 code asserted.
Live verification against the real API showed a substantial `wiki.summary` on an
album response, and the derivation was discarding it -- so album overviews, one of
the three metadata types this tool exists to edit, were silently unavailable.

The columns mirror the ones `lastfm_track` already has, because the same wiki shape
arrives for both.

`releasedate` and `production_year` are deliberately left in place: the current API
no longer returns `releasedate` for album.getInfo (checked by name and by MBID), so
both stay null, but a response that does carry it still parses rather than failing.

Revision ID: 0002_album_overview
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_album_overview"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lastfm_album", sa.Column("overview", sa.Text(), nullable=True))
    op.add_column("lastfm_album", sa.Column("wiki_published", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("lastfm_album", "wiki_published")
    op.drop_column("lastfm_album", "overview")
