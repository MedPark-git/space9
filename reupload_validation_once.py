from datetime import date, datetime, time
from decimal import Decimal
import json

MARKER = "REUPLOAD_VALIDATION_20260914_1TO8"
BASE_DATE = date(2026, 1, 1)
CUTOFF = date(2026, 8, 31)


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
        batch_rows = []
        for b in batches:
            batch_rows.append({
                "code": b.batch_code,
                "type": b.file_type,
                "start": b.query_start_date.isoformat() if b.query_start_date else None,
                "end": b.query_end_date.isoformat() if b.query_end_date else None,
                "total": b.total_rows,
                "new": b.new_rows,
                "duplicate": b.duplicate_rows,
                "review": b.review_rows,
                "error": b.error_rows,
            })

        all_txs = core.BankTransaction.query.all()
        total_count = len(all_txs)
        krw_count = sum(1 for t in all_txs if t.account.currency == "KRW")
        fx_count = total_count - krw_count
        min_date = min((t.transaction_date for t in all_txs), default=None)
        max_date = max((t.transaction_date for t in all_txs), default=None)
        tx_account_ids = {t.account_id for t in all_txs}

        obs = core.OpeningBalance.query.filter_by(base_date=BASE_DATE).all()
        results = []
        for ob in obs:
            txs = [t for t in all_txs if t.account_id == ob.bank_account_id and BASE_DATE <= t.transaction_date <= CUTOFF]
            opening = Decimal(ob.opening_balance or 0)
            deposits = sum((Decimal(t.deposit_amount) for t in txs), Decimal("0"))
            withdrawals = sum((Decimal(t.withdrawal_amount) for t in txs), Decimal("0"))
            calculated = opening + deposits - withdrawals
            reported_tx = _reported_tx(txs, ob.currency)
            reported = Decimal(reported_tx.balance) if reported_tx else None
            difference = calculated - reported if reported is not None else None
            results.append({
                "bank": ob.account.financial_institution,
                "account": ob.account.account_number,
                "currency": ob.currency,
                "opening": str(opening),
                "deposits": str(deposits),
                "withdrawals": str(withdrawals),
                "calculated": str(calculated),
                "reported": str(reported) if reported is not None else None,
                "difference": str(difference) if difference is not None else None,
                "reported_date": reported_tx.transaction_date.isoformat() if reported_tx else None,
                "match": difference == 0 if difference is not None else False,
                "has_data": bool(txs),
            })

        matched = [r for r in results if r["has_data"] and r["match"]]
        mismatched = [r for r in results if r["has_data"] and not r["match"]]
        no_data = [r for r in results if not r["has_data"]]
        batch_problem = [b for b in batch_rows if b["review"] or b["error"]]
        sum_new = sum(b["new"] for b in batch_rows)
        count_consistent = (sum_new == total_count)

        summary = {
            "marker": MARKER,
            "batch_count": len(batch_rows),
            "batches": batch_rows,
            "bank_transaction_total": total_count,
            "krw_count": krw_count,
            "fx_count": fx_count,
            "min_date": min_date.isoformat() if min_date else None,
            "max_date": max_date.isoformat() if max_date else None,
            "transaction_account_count": len(tx_account_ids),
            "opening_account_count": len(obs),
            "sum_batch_new": sum_new,
            "db_count_matches_batch_new": count_consistent,
            "batch_problem_count": len(batch_problem),
            "matched_count": len(matched),
            "mismatched_count": len(mismatched),
            "no_data_count": len(no_data),
        }

        existing = core.AuditLog.query.filter_by(action="reupload_validation_20260831", target_id=MARKER).first()
        if not existing and actor:
            db.session.add(core.AuditLog(
                user_id=actor.id,
                login_id=actor.login_id,
                action="reupload_validation_20260831",
                target_type="bank_transactions",
                target_id=MARKER,
                detail=(f"batches={len(batch_rows)} total={total_count} krw={krw_count} fx={fx_count} "
                        f"matched={len(matched)} mismatched={len(mismatched)} no_data={len(no_data)} "
                        f"batch_problem={len(batch_problem)} count_consistent={count_consistent}"),
                ip_address="system:reupload_validation",
            ))
            for r in mismatched:
                db.session.add(core.AuditLog(
                    user_id=actor.id,
                    login_id=actor.login_id,
                    action="reupload_reconciliation_mismatch",
                    target_type="bank_account",
                    target_id=f"{r['bank']}|{r['account']}|{r['currency']}",
                    detail=(f"opening={r['opening']} deposits={r['deposits']} withdrawals={r['withdrawals']} "
                            f"calculated={r['calculated']} reported={r['reported']} diff={r['difference']}"),
                    ip_address="system:reupload_validation",
                ))
            db.session.commit()

        print("MEDPARK_REUPLOAD_VALIDATION_SUMMARY " + json.dumps(summary, ensure_ascii=False, separators=(",", ":")), flush=True)
        for b in batch_rows:
            print("MEDPARK_REUPLOAD_BATCH " + json.dumps(b, ensure_ascii=False, separators=(",", ":")), flush=True)
        for r in mismatched:
            print("MEDPARK_REUPLOAD_MISMATCH " + json.dumps(r, ensure_ascii=False, separators=(",", ":")), flush=True)
        for r in no_data:
            print("MEDPARK_REUPLOAD_NO_DATA " + json.dumps({"bank":r["bank"],"account":r["account"],"currency":r["currency"],"opening":r["opening"]}, ensure_ascii=False, separators=(",", ":")), flush=True)
