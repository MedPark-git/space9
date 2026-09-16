from datetime import date
from sqlalchemy import func

import app as pkg
import phase2_feature as p2
import phase2_enhancements as p2e

app, core, db = pkg.app, pkg.core, pkg.db

REQUIRED_ENDPOINTS = {
    "dashboard", "cash_journal", "financial_transactions", "financial_transactions_csv", "mmt",
    "phase2_integrity", "phase2_accounts", "phase2_classifications", "phase2_cash_plans",
    "phase2_fx_rates", "phase2_cash_journal_excel", "phase2_cash_journal_action",
}

def validate():
    with app.app_context():
        active_categories = p2.TransactionCategory.query.filter_by(is_active=True).all()
        names = [x.name for x in active_categories]
        missing = [x for x in p2.CATEGORY_NAMES if x not in names]
        if missing:
            raise RuntimeError(f"PHASE2_VALIDATION_FAILED category_missing={missing}")
        if names.count("기타비용(기술)") != 1:
            raise RuntimeError("PHASE2_VALIDATION_FAILED duplicate 기타비용(기술)")
        tx_count = core.BankTransaction.query.count()
        ann_count = p2.TransactionAnnotation.query.count()
        if tx_count != ann_count:
            raise RuntimeError(f"PHASE2_VALIDATION_FAILED annotation_count tx={tx_count} ann={ann_count}")
        distinct_days = db.session.query(core.BankTransaction.account_id, core.BankTransaction.transaction_date).filter(core.BankTransaction.transaction_date >= p2.BASE_DATE).distinct().count()
        check_count = p2.DailyBalanceCheck.query.count()
        if check_count < distinct_days:
            raise RuntimeError(f"PHASE2_VALIDATION_FAILED daily_checks expected>={distinct_days} actual={check_count}")
        for model in (p2.BankAccountSetting, p2.CashPlan, p2.FxRate, p2.CashJournalVersion):
            model.query.limit(1).all()
        endpoints = set(app.view_functions)
        missing_ep = sorted(REQUIRED_ENDPOINTS - endpoints)
        if missing_ep:
            raise RuntimeError(f"PHASE2_VALIDATION_FAILED endpoints={missing_ep}")
        latest = p2._latest_transaction_date()
        if latest:
            snap = p2e._snapshot(latest)
            for key in ("krw_total","fx_krw_total","mmt_total","total_financial","expected","status","accounts"):
                if key not in snap:
                    raise RuntimeError(f"PHASE2_VALIDATION_FAILED snapshot_missing={key}")
        core.OpeningBalance.query.count(); core.ImportBatch.query.count(); core.ImportRawRow.query.count(); core.MmtTransaction.query.count(); core.LoanTransaction.query.count()
        print("PHASE2_VALIDATION_OK", f"categories={len(active_categories)} tx={tx_count} annotations={ann_count} daily_checks={check_count} latest={latest}", flush=True)
