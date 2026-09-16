from sqlalchemy import text

import app as pkg
import phase2_feature as p2
import phase21_fx_core as fx

core = pkg.core
db = pkg.db


def apply():
    def migrate():
        # fx_rates is an intentional Phase 2.1/backfill mutation target.
        # Protect only canonical Production datasets that must not change during schema migration.
        before = {
            "bank_transactions": core.BankTransaction.query.count(),
            "opening_balances": core.OpeningBalance.query.count(),
            "cash_journal_versions": p2.CashJournalVersion.query.count(),
            "import_batches": core.ImportBatch.query.count(),
            "import_raw_rows": core.ImportRawRow.query.count(),
        }
        db.session.rollback()
        stmts = [
            "ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS unit_amount NUMERIC(28,8)",
            "ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS rate_type VARCHAR(50)",
            "ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS source_date DATE",
            "ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS source_url VARCHAR(500)",
            "ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS retrieved_at TIMESTAMPTZ",
            "ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS response_hash VARCHAR(64)",
            "ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS ix_fx_rates_lookup21 ON fx_rates(base_date,currency,is_active)",
        ]
        with db.engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(874221941)"))
            for stmt in stmts:
                conn.execute(text(stmt))
        after = {
            "bank_transactions": core.BankTransaction.query.count(),
            "opening_balances": core.OpeningBalance.query.count(),
            "cash_journal_versions": p2.CashJournalVersion.query.count(),
            "import_batches": core.ImportBatch.query.count(),
            "import_raw_rows": core.ImportRawRow.query.count(),
        }
        db.session.rollback()
        if before != after:
            raise RuntimeError(f"Phase2.1 protected Production data guard failed: {before}->{after}")
    fx.migrate = migrate
