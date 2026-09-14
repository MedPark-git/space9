import base64, json, os, zlib
from datetime import date
from decimal import Decimal
from sqlalchemy import text

PATCH_ENV = "CASH_OPENING_BALANCE_PATCH_B64"
AUDIT_ACTION = "opening_balance_corrected_manual"


def _decode_payload():
    raw = os.getenv(PATCH_ENV)
    if not raw:
        return None
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(raw.encode())).decode("utf-8"))


def register():
    payload = _decode_payload()
    if not payload:
        return

    import app as pkg
    app, core, db = pkg.app, pkg.core, pkg.db

    with app.app_context():
        db.session.execute(text("SELECT pg_advisory_xact_lock(874221937)"))
        marker = str(payload.get("marker") or "")
        if not marker:
            raise RuntimeError("기초잔액 정정 marker가 없습니다.")

        existing = core.AuditLog.query.filter_by(action=AUDIT_ACTION, target_id=marker).first()
        if existing:
            db.session.rollback()
            return

        actor = core.User.query.filter_by(role="admin", is_active_flag=True).order_by(core.User.created_at).first() or core.User.query.first()
        if not actor:
            raise RuntimeError("기초잔액 정정용 사용자를 찾을 수 없습니다.")

        changes = []
        for item in payload.get("updates") or []:
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

        db.session.add(core.AuditLog(
            user_id=actor.id,
            login_id=actor.login_id,
            action=AUDIT_ACTION,
            target_type="opening_balance",
            target_id=marker,
            detail="; ".join(changes),
            ip_address="system:opening_balance_patch",
        ))
        db.session.commit()
        print("MEDPARK_OPENING_BALANCE_PATCH", marker, "; ".join(changes), flush=True)
