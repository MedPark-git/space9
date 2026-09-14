from datetime import date, time
from decimal import Decimal

BASE_DATE = date(2026, 1, 1)
CUTOFF = date(2026, 8, 31)
MARKER = "REUPLOAD_VALIDATION_20260914_1TO8"


def _reported_tx(txs, currency):
    if not txs:
        return None
    latest_date = max(t.transaction_date for t in txs)
    same = [t for t in txs if t.transaction_date == latest_date]
    if currency != "KRW":
        return min(same, key=lambda t: t.source_row_number)
    latest_time = max((t.transaction_time or time.min) for t in same)
    same_time = [t for t in same if (t.transaction_time or time.min) == latest_time]
    return min(same_time, key=lambda t: t.source_row_number)


def run_validation():
    import app as pkg
    core, db, app = pkg.core, pkg.db, pkg.app
    with app.app_context():
        actor = core.User.query.filter_by(role="admin", is_active_flag=True).order_by(core.User.created_at).first() or core.User.query.first()
        batches = core.ImportBatch.query.filter(core.ImportBatch.file_type.in_(["krw", "fx"])).order_by(core.ImportBatch.created_at).all()
        batch_problem = [b for b in batches if b.review_rows or b.error_rows]
        all_txs = core.BankTransaction.query.all()
        total_count = len(all_txs)
        sum_new = sum(b.new_rows for b in batches)
        mismatches = []

        obs = core.OpeningBalance.query.filter_by(base_date=BASE_DATE).all()
        for ob in obs:
            txs = [t for t in all_txs if t.account_id == ob.bank_account_id and BASE_DATE <= t.transaction_date <= CUTOFF]
            if not txs:
                continue
            opening = Decimal(ob.opening_balance or 0)
            deposits = sum((Decimal(t.deposit_amount) for t in txs), Decimal("0"))
            withdrawals = sum((Decimal(t.withdrawal_amount) for t in txs), Decimal("0"))
            calculated = opening + deposits - withdrawals
            reported_tx = _reported_tx(txs, ob.currency)
            reported = Decimal(reported_tx.balance)
            diff = calculated - reported
            if diff != 0:
                mismatches.append((ob.account.financial_institution, ob.account.account_number, ob.currency, opening, deposits, withdrawals, calculated, reported, diff))

        krw_count = sum(1 for t in all_txs if t.account.currency == "KRW")
        fx_count = total_count - krw_count
        detail = (f"batches={len(batches)} total={total_count} krw={krw_count} fx={fx_count} "
                  f"sum_new={sum_new} batch_problem={len(batch_problem)} mismatches={len(mismatches)}")
        existing = core.AuditLog.query.filter_by(action="reupload_validation_20260831", target_id=MARKER).first()
        if not existing and actor:
            db.session.add(core.AuditLog(user_id=actor.id, login_id=actor.login_id, action="reupload_validation_20260831", target_type="bank_transactions", target_id=MARKER, detail=detail, ip_address="system:reupload_validation"))
            for m in mismatches:
                db.session.add(core.AuditLog(user_id=actor.id, login_id=actor.login_id, action="reupload_reconciliation_mismatch", target_type="bank_account", target_id=f"{m[0]}|{m[1]}|{m[2]}", detail=f"opening={m[3]} deposits={m[4]} withdrawals={m[5]} calculated={m[6]} reported={m[7]} diff={m[8]}", ip_address="system:reupload_validation"))
            db.session.commit()

        problems = []
        if not batches:
            problems.append("원화/외화 Import Batch 없음")
        if batch_problem:
            problems.append("Batch 오류/확인필요=" + ",".join(f"{b.batch_code}(review={b.review_rows},error={b.error_rows})" for b in batch_problem))
        if sum_new != total_count:
            problems.append(f"Batch 신규건수 합({sum_new}) != DB 거래건수({total_count})")
        if mismatches:
            problems.append("잔액불일치=" + ";".join(f"{m[0]} {m[1]} {m[2]} calc={m[6]} bank={m[7]} diff={m[8]}" for m in mismatches[:10]))
        if problems:
            raise RuntimeError("REUPLOAD_VALIDATION_FAILED | " + " | ".join(problems))
