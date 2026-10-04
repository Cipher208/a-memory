"""memory_dispatch_rejections — the refusal counter (2026-10-04).

`memory_dispatch_log` records what was SAVED. Every guard in `auto_save_text` and
every block in the middleware pipeline returns before its insert, so a refused
message left no row anywhere. Measured on a live base: 1419 dispatch rows, not
one with `score = 0.0` — the refusals were simply absent.

That silence hid the sixth bug: `_TRANSCRIPT_HEAD` carried the whole markdown
set, so the intake door discarded 591 of one persona's 734 substantive messages
(80% of her voice), and the loss was findable only by re-deriving the number by
hand from the source database. With this table it is a rate change: one or two
refusals a day, then 591 in a single window.

One row per refusal. The refused text is NOT stored — only a short preview
(`shared.door_log.PREVIEW_CHARS`), because keeping the text would put back
exactly what the guard declined to keep, and because this table must never
become a recall source. It is written best-effort and read only by operators.

Volume is bounded by the source, not by the guard: normal operation runs at one
or two refusals a day, and a `below_importance_threshold` row is the designed
filter doing its job rather than a defect — which is why every row carries the
reason and readers group by it.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261004_1300_door_rejections"
down_revision: str | Sequence[str] | None = "20260919_1400_g25_epi_edges_target_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS memory_dispatch_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL DEFAULT '',
            source_msg_id INTEGER,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL DEFAULT 'default',
            reason TEXT NOT NULL,
            text_preview TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_rejections_user_time ON memory_dispatch_rejections(user_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_rejections_reason ON memory_dispatch_rejections(reason)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_rejections_reason")
    op.execute("DROP INDEX IF EXISTS idx_rejections_user_time")
    op.execute("DROP TABLE IF EXISTS memory_dispatch_rejections")
