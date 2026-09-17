import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, text

import app as pkg
import phase2_feature as p2

core = pkg.core
db = pkg.db
BASE_DATE = date(2026, 1, 1)
MIGRATION_LOCK = 874221952


def _dec(v):
    return Decimal(str(v or 0))


def _norm(v):
    return re.sub(r"[^0-9A-Za-z]", "", str(v or "")).upper()


def migrate_schema():
    stmts = [
        "ALTER TABLE daily_balance_checks ADD COLUMN IF NOT EXISTS previous_balance NUMERIC(28,6)",
        "ALTER TABLE daily_balance_checks ADD COLUMN IF NOT EXISTS deposit_total NUMERIC(28,6)",
        "ALTER TABLE daily_balance_checks ADD COLUMN IF NOT EXISTS withdrawal_total NUMERIC(28,6)",
        "ALTER TABLE daily_balance_checks ADD COLUMN IF NOT EXISTS source_last_transaction_id UUID",
        "ALTER TABLE daily_balance_checks ADD COLUMN IF NOT EXISTS calculated_at TIMESTAMPTZ",
        "ALTER TABLE daily_balance_checks ADD COLUMN IF NOT EXISTS is_carry_forward BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE transaction_annotations ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_daily_balance_checks_date_account ON daily_balance_checks(check_date, bank_account_id)",
    ]
    db.session.rollback()
    with db.engine.begin() as conn:
        conn.execute(text(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK})"))
        for stmt in stmts:
            conn.execute(text(stmt))


def _final_from_rows(rows, currency):
    if not rows:
        return None
    if currency == "KRW":
        timed = [t for t in rows if t.transaction_time is not None]
        if timed:
            max_time = max(t.transaction_time for t in timed)
            rows = [t for t in timed if t.transaction_time == max_time]
        return min(rows, key=lambda t: (t.source_row_number, str(t.id)))
    return min(rows, key=lambda t: (t.source_row_number, str(t.id)))


def final_bank_tx_on_day(account_id, currency, day):
    rows = core.BankTransaction.query.filter_by(account_id=account_id, transaction_date=day).all()
    return _final_from_rows(rows, currency)


def final_bank_tx_at_or_before(account, cutoff):
    latest_day = db.session.query(func.max(core.BankTransaction.transaction_date)).filter(
        core.BankTransaction.account_id == account.id,
        core.BankTransaction.transaction_date <= cutoff,
    ).scalar()
    if not latest_day:
        return None
    return final_bank_tx_on_day(account.id, account.currency, latest_day)


def final_mmt_tx_on_day(bank, account_no, currency, day):
    rows = core.MmtTransaction.query.filter_by(
        financial_institution=bank,
        account_number=account_no,
        currency=currency,
        transaction_date=day,
    ).all()
    if not rows:
        return None
    timed = [t for t in rows if t.transaction_time is not None]
    if timed:
        mt = max(t.transaction_time for t in timed)
        rows = [t for t in timed if t.transaction_time == mt]
    return min(rows, key=lambda t: (t.source_row_number, str(t.id)))


def mmt_balance(bank, account_no, currency, cutoff):
    ob = pkg.MmtOpeningBalance.query.filter_by(
        base_date=BASE_DATE,
        financial_institution=bank,
        account_number=account_no,
        currency=currency,
    ).first()
    opening = _dec(ob.opening_balance) if ob else None
    dep, wd = db.session.query(
        func.coalesce(func.sum(core.MmtTransaction.deposit_amount), 0),
        func.coalesce(func.sum(core.MmtTransaction.withdrawal_amount), 0),
    ).filter(
        core.MmtTransaction.financial_institution == bank,
        core.MmtTransaction.account_number == account_no,
        core.MmtTransaction.currency == currency,
        core.MmtTransaction.transaction_date >= BASE_DATE,
        core.MmtTransaction.transaction_date <= cutoff,
    ).one()
    dep, wd = _dec(dep), _dec(wd)
    calc = opening + dep - wd if opening is not None else None
    latest_day = db.session.query(func.max(core.MmtTransaction.transaction_date)).filter(
        core.MmtTransaction.financial_institution == bank,
        core.MmtTransaction.account_number == account_no,
        core.MmtTransaction.currency == currency,
        core.MmtTransaction.transaction_date <= cutoff,
    ).scalar()
    last = final_mmt_tx_on_day(bank, account_no, currency, latest_day) if latest_day else None
    reported = _dec(last.balance) if last else None
    if opening is None:
        status, diff = "insufficient", None
    elif reported is None:
        status, diff = "ok", Decimal("0")
        reported = calc
    else:
        diff = calc - reported
        status = "ok" if diff == 0 else "review"
    return {
        "opening": opening, "deposit": dep, "withdrawal": wd,
        "calculated": calc, "reported": reported, "difference": diff,
        "status": status, "reported_date": latest_day,
    }


def _internal_account_numbers():
    nums = set()
    for a in core.BankAccount.query.all():
        s = p2.BankAccountSetting.query.filter_by(bank_account_id=a.id).first()
        if s is None or s.is_internal:
            nums.add(_norm(a.account_number))
    for row in db.session.query(core.MmtTransaction.account_number).distinct().all():
        nums.add(_norm(row[0]))
    for row in pkg.MmtOpeningBalance.query.filter_by(base_date=BASE_DATE).all():
        nums.add(_norm(row.account_number))
    return {x for x in nums if x}


def ensure_annotations_for_ids(transaction_ids):
    ids = list(transaction_ids)
    if not ids:
        return 0
    existing = {x[0] for x in db.session.query(p2.TransactionAnnotation.transaction_id).filter(
        p2.TransactionAnnotation.transaction_id.in_(ids)
    ).all()}
    internal = _internal_account_numbers()
    created = 0
    for tx in core.BankTransaction.query.filter(core.BankTransaction.id.in_(ids)).all():
        if tx.id in existing:
            continue
        counter = _norm(tx.counter_account)
        if counter and counter in internal:
            flow = "internal_in" if _dec(tx.deposit_amount) > 0 else "internal_out" if _dec(tx.withdrawal_amount) > 0 else "internal"
        else:
            flow = "external_in" if _dec(tx.deposit_amount) > 0 else "external_out" if _dec(tx.withdrawal_amount) > 0 else "external"
        suggested_id, reason = p2._past_suggestion(tx)
        db.session.add(p2.TransactionAnnotation(
            transaction_id=tx.id,
            flow_type=flow,
            suggested_category_id=suggested_id,
            suggestion_reason=reason,
            classification_source="rule" if suggested_id or flow.startswith("internal") else "manual",
        ))
        created += 1
    return created


def _daily_row(account_id, day):
    return p2.DailyBalanceCheck.query.filter_by(bank_account_id=account_id, check_date=day).first()


def recalc_account(account_id, start_date=None, through_date=None):
    account = db.session.get(core.BankAccount, account_id)
    if not account:
        return 0
    ob = core.OpeningBalance.query.filter_by(base_date=BASE_DATE, bank_account_id=account.id).first()
    if not ob or ob.opening_balance is None:
        return 0
    opening = _dec(ob.opening_balance)
    global_latest = db.session.query(func.max(core.BankTransaction.transaction_date)).scalar()
    if through_date is None:
        through_date = global_latest
    if through_date is None or through_date < BASE_DATE:
        return 0
    start_date = max(BASE_DATE, start_date or BASE_DATE)
    if start_date > through_date:
        return 0

    if start_date == BASE_DATE:
        previous = opening
        cumulative_dep = Decimal("0")
        cumulative_wd = Decimal("0")
        last_source_id = None
    else:
        prev_row = p2.DailyBalanceCheck.query.filter(
            p2.DailyBalanceCheck.bank_account_id == account.id,
            p2.DailyBalanceCheck.check_date < start_date,
        ).order_by(p2.DailyBalanceCheck.check_date.desc()).first()
        if prev_row and prev_row.calculated_balance is not None:
            previous = _dec(prev_row.calculated_balance)
            cumulative_dep = _dec(prev_row.cumulative_deposit)
            cumulative_wd = _dec(prev_row.cumulative_withdrawal)
            extra = db.session.execute(text(
                "SELECT source_last_transaction_id FROM daily_balance_checks WHERE id=:id"
            ), {"id": prev_row.id}).first()
            last_source_id = extra[0] if extra else None
        else:
            dep, wd = db.session.query(
                func.coalesce(func.sum(core.BankTransaction.deposit_amount), 0),
                func.coalesce(func.sum(core.BankTransaction.withdrawal_amount), 0),
            ).filter(
                core.BankTransaction.account_id == account.id,
                core.BankTransaction.transaction_date >= BASE_DATE,
                core.BankTransaction.transaction_date < start_date,
            ).one()
            cumulative_dep, cumulative_wd = _dec(dep), _dec(wd)
            previous = opening + cumulative_dep - cumulative_wd
            prior_tx = final_bank_tx_at_or_before(account, start_date - timedelta(days=1))
            last_source_id = prior_tx.id if prior_tx else None

    txs = core.BankTransaction.query.filter(
        core.BankTransaction.account_id == account.id,
        core.BankTransaction.transaction_date >= start_date,
        core.BankTransaction.transaction_date <= through_date,
    ).all()
    by_day = defaultdict(list)
    for tx in txs:
        by_day[tx.transaction_date].append(tx)

    updated = 0
    day = start_date
    while day <= through_date:
        items = by_day.get(day, [])
        dep = sum((_dec(t.deposit_amount) for t in items), Decimal("0"))
        wd = sum((_dec(t.withdrawal_amount) for t in items), Decimal("0"))
        cumulative_dep += dep
        cumulative_wd += wd
        calculated = previous + dep - wd
        final_tx = _final_from_rows(items, account.currency) if items else None
        if final_tx:
            reported = _dec(final_tx.balance)
            last_source_id = final_tx.id
            difference = calculated - reported
            status = "ok" if difference == 0 else "review"
            carry = False
        else:
            reported = calculated
            difference = Decimal("0")
            status = "ok"
            carry = True

        row = _daily_row(account.id, day)
        if not row:
            row = p2.DailyBalanceCheck(bank_account_id=account.id, check_date=day)
            db.session.add(row)
            db.session.flush()
        row.opening_balance = opening
        row.cumulative_deposit = cumulative_dep
        row.cumulative_withdrawal = cumulative_wd
        row.calculated_balance = calculated
        row.bank_reported_balance = reported
        row.difference = difference
        row.status = status
        row.checked_at = core.utcnow()
        db.session.flush()
        db.session.execute(text("""
            UPDATE daily_balance_checks
               SET previous_balance=:prev, deposit_total=:dep, withdrawal_total=:wd,
                   source_last_transaction_id=:src, calculated_at=:at, is_carry_forward=:carry
             WHERE id=:id
        """), {
            "prev": previous, "dep": dep, "wd": wd, "src": last_source_id,
            "at": core.utcnow(), "carry": carry, "id": row.id,
        })
        previous = calculated
        updated += 1
        day += timedelta(days=1)
    return updated


def recalc_accounts(account_ids, start_by_account=None, through_date=None):
    total = 0
    for aid in set(account_ids):
        start = (start_by_account or {}).get(aid, BASE_DATE)
        total += recalc_account(aid, start, through_date)
    return total


def recalc_all_accounts(through_date=None):
    ids = [x[0] for x in db.session.query(core.BankAccount.id).all()]
    return recalc_accounts(ids, {x: BASE_DATE for x in ids}, through_date)


def post_import_for_batch(batch_id, actor=None):
    rows = core.BankTransaction.query.filter_by(import_batch_id=batch_id).all()
    if not rows:
        return {"transactions": 0, "annotations": 0, "daily_rows": 0}
    ids = [t.id for t in rows]
    annotations = ensure_annotations_for_ids(ids)
    starts = {}
    for t in rows:
        starts[t.account_id] = min(starts.get(t.account_id, t.transaction_date), t.transaction_date)
    daily_rows = recalc_accounts(starts.keys(), starts)
    if actor:
        core.audit(
            "phase25_import_postprocess", user=actor,
            target_type="import_batch", target_id=batch_id,
            detail=f"transactions={len(rows)}, annotations={annotations}, daily_rows={daily_rows}",
        )
    return {"transactions": len(rows), "annotations": annotations, "daily_rows": daily_rows}


def _post_import_current_request():
    latest = core.ImportBatch.query.order_by(core.ImportBatch.confirmed_at.desc()).first()
    if not latest or latest.file_type not in {"krw", "fx"}:
        return
    post_import_for_batch(latest.id, current_actor())
    db.session.commit()


def current_actor():
    try:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            return current_user
    except Exception:
        pass
    return None


def install_patches():
    p2._reported_tx = final_bank_tx_at_or_before
    p2._mmt_balance = mmt_balance
    p2._post_import = _post_import_current_request


def assert_category_master_unchanged():
    names = [x.name for x in p2.TransactionCategory.query.filter_by(is_active=True).order_by(p2.TransactionCategory.sort_order).all()]
    if len(names) != 33 or names.count("기타비용(기술)") != 1:
        raise RuntimeError(f"Phase2.5 category master invariant failed: count={len(names)}")
