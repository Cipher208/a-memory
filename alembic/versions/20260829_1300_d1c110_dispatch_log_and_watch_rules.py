"""Phase C C1.10 — memory_dispatch_log + watch_rules (operator introspection).

Revision ID: 20260829_1300_d1c110
Revises: e5b7d2f8a1c3
Create Date: 2026-08-29 13:00:00.000000

Two new tables:
- memory_dispatch_log: one row per dispatched save event. Used by the
  post_session_diff auto-handler to surface gaps; also powers the
  memory_watch tool's hits_24h counter.
- watch_rules: named declarations of the rules ariel already applies via
  auto_save_text. Operator CRUD via the memory_watch tool; rules do not
  introduce new behavior (no runtime action-executor).

Seeded: one default rule per active auto-handler (mirrors auto_save_text
defaults: min_importance 0.5; the L4-only band at 0.8 is encoded by the
two-tier predicate {min_importance: 0.5, l4_min_importance: 0.8}).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_1300_d1c110"
down_revision: str | Sequence[str] | None = "e5b7d2f8a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS memory_dispatch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            source_msg_id INTEGER,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL DEFAULT 'default',
            score REAL,
            saved_l3 INTEGER NOT NULL DEFAULT 0,
            saved_l4 INTEGER NOT NULL DEFAULT 0,
            saved_graph INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_log_user_time ON memory_dispatch_log(user_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_log_event      ON memory_dispatch_log(event)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS watch_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            trigger TEXT NOT NULL,
            predicate TEXT NOT NULL,
            action TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        )
    """)
    op.execute("""
        INSERT OR IGNORE INTO watch_rules (name, trigger, predicate, action, enabled, created_at)
        VALUES (
            'auto_save_default',
            'new_message',
            '{"min_importance": 0.5, "l4_min_importance": 0.8}',
            'auto_save_text',
            1,
            strftime('%s','now')
        )
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dispatch_log_event")
    op.execute("DROP INDEX IF EXISTS idx_dispatch_log_user_time")
    op.execute("DROP TABLE IF EXISTS memory_dispatch_log")
    op.execute("DROP TABLE IF EXISTS watch_rules")
