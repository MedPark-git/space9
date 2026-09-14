import base64, json, os, zlib
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import text

PATCH_ENV = "CASH_OPENING_BALANCE_PATCH_B64"
AUDIT_ACTION = "opening_balance_corrected_manual"


def _decode_payload():
    raw = os.getenv(PATCH_ENV)
    if not raw:
        return None
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(raw.encode())).decode("utf-8"))


def _verify(core, updates):
    cutoff = date(2026, 8, 31)
    results = []
    for item in updates:
        bank = str(item["bank"])
        account_no = str(item["account_number"])
        currency = str(item.get("currency") or "KRW").upper()
        base_date = date.fromisoformat(str(item.get("base_date") or "2026-01-01"))
        expected = Decimal(str(item["opening_balance"]))

        account = core.BankAccount.query.filter_by(
            financial_institution=bank,
            account_number=account_no,
            currency=currency,
        ).first()
        if not account:
            raise RuntimeError(f"검증 실패 - 계좌 Master 없음: {bank} {account_no} {currency}")

        ob = core.OpeningBalance.query.filter_by(base_date=base_date, bank_account_id=account.id).first()
        if not ob:
            raise RuntimeError(f"검증 실패 - 기초잔액 레코드 없음: {bank} {account_no} {currency}")
        actual = Decimal(ob.opening_balance or 0)
        if actual != expected:
            raise RuntimeError(f"검증 실패 - 기초잔액 불일치: {account_no} expected={expected} actual={actual}")

        txs = core.BankTransaction.query.filter(
            core.BankTransaction.account_id == account.id,
            core.BankTransaction.transaction_date >= base_date,
            core.BankTransaction.transaction_date <= cutoff,
        ).all()
        if not txs:
            raise RuntimeError(f"검증 실패 - 8월말 대사용 거래내역 없음: {account_no}")

        latest_date = max(t.transaction_date for t in txs)
        same_day = [t for t in txs if t.transaction_date == latest_date]
        reported = max(same_day, key=lambda t: (t.transaction_time or datetime.min.time(), t.source_row_number)).balance
        calculated = actual + sum((Decimal(t.deposit_amount) for t in txs), Decimal(0)) - sum((Decimal(t.withdrawal_amount) for t in txs), Decimal(0))
        if calculated != Decimal(reported):
            raise RuntimeError(
                f"검증 실패 - 8월말 잔액 불일치: {account_no} calculated={calculated} reported={reported}"
            )
        results.append(f"{account_no}:opening={actual},aug31={reported},match=true")
    return results


def register():
    payload = _decode_payload()
    if not payload:
        return

    import app as pkg
    app, core, db = pkg.app, pkg.core, pkg.db

    with app.app_context():
        db.session.execute(text("SELECT pg_advisory_xact_lock(874221937)"))
        marker = str(payload.get("marker") or "")
        updates = payload.get("updates") or []
        if not marker:
            raise RuntimeError("기초잔액 정정 marker가 없습니다.")
        if not updates:
            raise RuntimeError("기초잔액 정정 대상이 없습니다.")

        existing = core.AuditLog.query.filter_by(action=AUDIT_ACTION, target_id=marker).first()
        if existing:
            results = _verify(core, updates)
            db.session.rollback()
            print("MEDPARK_OPENING_BALANCE_VERIFY", marker, "; ".join(results), flush=True)
            return

        actor = core.User.query.filter_by(role="admin", is_active_flag=True).order_by(core.User.created_at).first() or core.User.query.first()
        if not actor:
            raise RuntimeError("기초잔액 정정용 사용자를 찾을 수 없습니다.")

        changes = []
        for item in updates:
            bank = str(item["bank"])
            account_no = str(item["account_number"])
            currency = str(item.get("currency") or "KRW").upper()
            base_date = date.fromisoformat(str(item.get("base_date") or "2026-01-01"))
            new_balance = Decimal(str(item["opening_balance"]))

            account = core.BankAccount.query.filter_by(
                financial_institution=bank,
                account_number=account_no,
                currency=currency,
            ).first()
            if not account:
                raise RuntimeError(f"계좌 Master 없음: {bank} {account_no} {currency}")

            ob = core.OpeningBalance.query.filter_by(base_date=base_date, bank_account_id=account.id).first()
            if not ob:
                raise RuntimeError(f"기초잔액 레코드 없음: {bank} {account_no} {currency}")

            old_balance = Decimal(ob.opening_balance or 0)
            ob.opening_balance = new_balance
            ob.status = "confirmed_manual"
            ob.calculation_method = "사용자 정정 확정 - 2026년 신규개설계좌 거래내역 재검증"
            ob.source_date = None
            ob.source_balance = None
            ob.source_transaction_id = None
            changes.append(f"{account_no}:{old_balance}->{new_balance}")

        db.session.flush()
        results = _verify(core, updates)
        db.session.add(core.AuditLog(
            user_id=actor.id,
            login_id=actor.login_id,
            action=AUDIT_ACTION,
            target_type="opening_balance",
            target_id=marker,
            detail="; ".join(changes) + " | verify=" + "; ".join(results),
            ip_address="system:opening_balance_patch",
        ))
        db.session.commit()
        print("MEDPARK_OPENING_BALANCE_PATCH", marker, "; ".join(changes), "; ".join(results), flush=True)
