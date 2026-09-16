from sqlalchemy import inspect, func


def register():
    import app as pkg
    app, core, db = pkg.app, pkg.core, pkg.db
    with app.app_context():
        inspector = inspect(db.engine)
        tables = sorted(inspector.get_table_names())
        bank_total = core.BankAccount.query.count()
        krw_accounts = core.BankAccount.query.filter_by(currency='KRW').count()
        fx_accounts = core.BankAccount.query.filter(core.BankAccount.currency != 'KRW').count()
        bank_tx_count = core.BankTransaction.query.count()
        tx_min, tx_max = db.session.query(func.min(core.BankTransaction.transaction_date), func.max(core.BankTransaction.transaction_date)).one()
        opening_count = core.OpeningBalance.query.count()
        opening_manual = core.OpeningBalance.query.filter_by(status='confirmed_manual').count()
        mmt_count = core.MmtTransaction.query.count()
        mmt_accounts = db.session.query(core.MmtTransaction.financial_institution, core.MmtTransaction.account_number, core.MmtTransaction.currency).distinct().count()
        loan_accounts = core.LoanAccount.query.count()
        loan_tx_count = core.LoanTransaction.query.count()
        import_count = core.ImportBatch.query.count()
        import_stats = db.session.query(core.ImportBatch.file_type, func.count(core.ImportBatch.id), func.sum(core.ImportBatch.new_rows), func.sum(core.ImportBatch.duplicate_rows), func.sum(core.ImportBatch.review_rows), func.sum(core.ImportBatch.error_rows)).group_by(core.ImportBatch.file_type).order_by(core.ImportBatch.file_type).all()
        cash_journal_count = core.CashJournal.query.count()
        audit_count = core.AuditLog.query.count()
        print('PHASE2_AUDIT tables=' + ','.join(tables), flush=True)
        print(f'PHASE2_AUDIT bank_accounts total={bank_total} krw={krw_accounts} fx={fx_accounts}', flush=True)
        print(f'PHASE2_AUDIT bank_transactions count={bank_tx_count} period={tx_min}~{tx_max}', flush=True)
        print(f'PHASE2_AUDIT opening_balances count={opening_count} manual={opening_manual}', flush=True)
        print(f'PHASE2_AUDIT mmt accounts={mmt_accounts} transactions={mmt_count}', flush=True)
        print(f'PHASE2_AUDIT loans accounts={loan_accounts} transactions={loan_tx_count}', flush=True)
        print(f'PHASE2_AUDIT import_batches count={import_count} stats={import_stats}', flush=True)
        print(f'PHASE2_AUDIT cash_journals={cash_journal_count} audit_logs={audit_count}', flush=True)
        db.session.rollback()
