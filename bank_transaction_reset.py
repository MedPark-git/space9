from pathlib import Path
from sqlalchemy import text

MARKER = "TEST_RESET_BANK_TX_20260914"
TARGET_TYPES = ("krw", "fx", "text_correction")


def register():
    import app as pkg
    app, core, db = pkg.app, pkg.core, pkg.db

    with app.app_context():
        db.session.execute(text("SELECT pg_advisory_xact_lock(874221938)"))

        existing = core.AuditLog.query.filter_by(
            action="bank_transaction_test_reset",
            target_id=MARKER,
        ).first()
        if existing:
            remaining = core.BankTransaction.query.count()
            if remaining != 0:
                raise RuntimeError(f"거래내역 초기화 검증 실패: remaining_bank_transactions={remaining}")
            db.session.rollback()
            print("MEDPARK_BANK_TX_RESET_VERIFY", f"remaining={remaining}", flush=True)
            return

        actor = (
            core.User.query.filter_by(role="admin", is_active_flag=True).order_by(core.User.created_at).first()
            or core.User.query.first()
        )
        if not actor:
            raise RuntimeError("거래내역 초기화 감사로그용 사용자를 찾을 수 없습니다.")

        before_tx = core.BankTransaction.query.count()
        before_accounts = core.BankAccount.query.count()
        before_opening = core.OpeningBalance.query.count()

        batches = core.ImportBatch.query.filter(core.ImportBatch.file_type.in_(TARGET_TYPES)).all()
        batch_ids = [b.id for b in batches]
        stored_paths = [b.stored_path for b in batches if b.stored_path]

        tx_ids = [row[0] for row in db.session.query(core.BankTransaction.id).all()]
        if tx_ids:
            core.OpeningBalance.query.filter(core.OpeningBalance.source_transaction_id.in_(tx_ids)).update(
                {core.OpeningBalance.source_transaction_id: None}, synchronize_session=False
            )
        if batch_ids:
            core.OpeningBalance.query.filter(core.OpeningBalance.source_batch_id.in_(batch_ids)).update(
                {core.OpeningBalance.source_batch_id: None}, synchronize_session=False
            )

        deleted_tx = core.BankTransaction.query.delete(synchronize_session=False)

        deleted_raw = 0
        if batch_ids:
            deleted_raw = core.ImportRawRow.query.filter(core.ImportRawRow.import_batch_id.in_(batch_ids)).delete(
                synchronize_session=False
            )

        deleted_batches = 0
        if batch_ids:
            deleted_batches = core.ImportBatch.query.filter(core.ImportBatch.id.in_(batch_ids)).delete(
                synchronize_session=False
            )

        after_tx = core.BankTransaction.query.count()
        after_accounts = core.BankAccount.query.count()
        after_opening = core.OpeningBalance.query.count()

        if after_tx != 0:
            raise RuntimeError(f"거래내역 초기화 실패: remaining_bank_transactions={after_tx}")
        if after_accounts != before_accounts:
            raise RuntimeError(f"계좌 Master 보존 실패: before={before_accounts}, after={after_accounts}")
        if after_opening != before_opening:
            raise RuntimeError(f"기초잔액 보존 실패: before={before_opening}, after={after_opening}")

        db.session.add(core.AuditLog(
            user_id=actor.id,
            login_id=actor.login_id,
            action="bank_transaction_test_reset",
            target_type="bank_transactions",
            target_id=MARKER,
            detail=(
                f"deleted_bank_transactions={deleted_tx}; deleted_raw_rows={deleted_raw}; "
                f"deleted_import_batches={deleted_batches}; preserved_bank_accounts={after_accounts}; "
                f"preserved_opening_balances={after_opening}; target_types={','.join(TARGET_TYPES)}"
            ),
            ip_address="system:test_reset",
        ))
        db.session.commit()

        for p in stored_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass

        print(
            "MEDPARK_BANK_TX_RESET",
            f"before={before_tx}",
            f"deleted={deleted_tx}",
            f"remaining={after_tx}",
            f"accounts={after_accounts}",
            f"opening={after_opening}",
            flush=True,
        )
