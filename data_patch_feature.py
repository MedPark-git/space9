import base64, hashlib, json, os, zlib
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import text

PATCH_ENV = "CASH_DATA_PATCH_B64"
OPENING_MARKER = "MedPark_2026_기초잔액_일괄등록_양식(1).xlsx"
CORRECTION_MARKER = "MANUAL_TEXT_CORRECTION_20260914_011_025.txt"


def _decode_payload():
    raw = os.getenv(PATCH_ENV)
    if not raw:
        return None
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(raw.encode())).decode("utf-8"))


def _sha(parts):
    return hashlib.sha256("|".join(str(x or "").strip() for x in parts).encode("utf-8")).hexdigest()


def register():
    payload = _decode_payload()
    if not payload:
        return
    import app as pkg
    core, db = pkg.core, pkg.db
    db.metadata.create_all(bind=db.engine)
    db.session.execute(text("SELECT pg_advisory_xact_lock(874221936)"))
    actor = core.User.query.filter_by(role="admin", is_active_flag=True).order_by(core.User.created_at).first() or core.User.query.first()
    if not actor:
        raise RuntimeError("데이터 패치 실행용 활성 사용자를 찾을 수 없습니다.")

    opening_batch = core.ImportBatch.query.filter_by(file_type="opening_balance", original_filename=OPENING_MARKER).first()
    if not opening_batch:
        opening_rows = payload.get("opening_rows") or []
        code = core.next_batch_code()
        opening_batch = core.ImportBatch(
            batch_code=code, file_type="opening_balance", original_filename=OPENING_MARKER,
            stored_path=None, file_sha256=_sha([json.dumps(opening_rows, ensure_ascii=False)]),
            structure_hash=_sha(["구분","금융기관","계좌번호","계좌명","통화","기준일","기초잔액","비고"]),
            query_start_date=date(2026,1,1), query_end_date=date(2026,1,1), total_rows=len(opening_rows),
            new_rows=len(opening_rows), duplicate_rows=0, review_rows=0, error_rows=0,
            status="confirmed", created_by=actor.id, confirmed_at=core.utcnow())
        db.session.add(opening_batch); db.session.flush()
        cache = {}
        new_count = update_count = 0
        for idx, row in enumerate(opening_rows, start=6):
            kind, bank, account_no, account_name, currency, base_text, bal_text, note = row
            bal = Decimal(str(bal_text)); base_date = datetime.strptime(base_text, "%Y-%m-%d").date()
            raw = {"구분":kind,"금융기관":bank,"계좌번호":account_no,"계좌명":account_name,"통화":currency,"기준일":base_text,"기초잔액":str(bal),"비고":note}
            if kind == "MMT":
                ob = pkg.MmtOpeningBalance.query.filter_by(base_date=base_date, financial_institution=bank, account_number=account_no, currency=currency).first()
                if ob: update_count += 1
                else:
                    ob = pkg.MmtOpeningBalance(base_date=base_date, financial_institution=bank, account_number=account_no, account_name=account_name, currency=currency, opening_balance=bal, created_by=actor.id); db.session.add(ob); new_count += 1
                ob.account_name=account_name or ob.account_name; ob.opening_balance=bal; ob.status="confirmed_manual"; ob.calculation_method="기초잔액 Excel 일괄등록(사용자 확정)"; ob.source_batch_id=opening_batch.id
            else:
                key=(bank,account_no,currency)
                account=cache.get(key) or core.BankAccount.query.filter_by(financial_institution=bank,account_number=account_no,currency=currency).first()
                if not account:
                    account=core.BankAccount(financial_institution=bank,account_number=account_no,account_name=account_name,currency=currency,account_type="deposit"); db.session.add(account); db.session.flush()
                cache[key]=account
                if account_name: account.account_name=account_name
                ob=core.OpeningBalance.query.filter_by(base_date=base_date,bank_account_id=account.id).first()
                if ob: update_count += 1
                else:
                    ob=core.OpeningBalance(base_date=base_date,bank_account_id=account.id,currency=currency,created_by=actor.id); db.session.add(ob); new_count += 1
                ob.source_date=None; ob.source_balance=None; ob.opening_balance=bal; ob.status="confirmed_manual"; ob.calculation_method="기초잔액 Excel 일괄등록(사용자 확정)"; ob.source_batch_id=opening_batch.id; ob.source_transaction_id=None
            db.session.add(core.ImportRawRow(import_batch_id=opening_batch.id, source_row_number=idx, row_status="confirmed", message="사용자 확정 기초잔액", raw_data=raw))
        opening_batch.new_rows=new_count; opening_batch.duplicate_rows=update_count
        db.session.add(core.AuditLog(user_id=actor.id,login_id=actor.login_id,action="opening_balance_excel_confirmed",target_type="import_batch",target_id=str(opening_batch.id),detail=f"{code} rows={len(opening_rows)} new={new_count} update={update_count}",ip_address="system:data_patch"))

    correction_batch = core.ImportBatch.query.filter_by(file_type="text_correction", original_filename=CORRECTION_MARKER).first()
    if not correction_batch:
        corrections = payload.get("corrections") or {}
        all_rows = [(acct,r) for acct, rows in corrections.items() for r in rows]
        code = core.next_batch_code(); dates=[datetime.strptime(r["datetime"],"%Y-%m-%d %H:%M:%S").date() for _,r in all_rows]
        correction_batch=core.ImportBatch(batch_code=code,file_type="text_correction",original_filename=CORRECTION_MARKER,stored_path=None,file_sha256=_sha([json.dumps(corrections,ensure_ascii=False)]),structure_hash=_sha(["거래일시","출금","입금","거래후 잔액","거래내용","상대계좌번호","상대은행","메모","거래구분","수표어음금액","CMS코드","상대계좌예금주명"]),query_start_date=min(dates),query_end_date=max(dates),total_rows=len(all_rows),new_rows=len(all_rows),duplicate_rows=0,review_rows=0,error_rows=0,status="confirmed",created_by=actor.id,confirmed_at=core.utcnow())
        db.session.add(correction_batch); db.session.flush()
        account_names={"141-080730-01-011":"중진공정책자금","141-080730-04-025":"글로벌마케팅"}
        audit_details=[]
        for account_no, rows in corrections.items():
            account=core.BankAccount.query.filter_by(financial_institution="기업",account_number=account_no,currency="KRW").first()
            if not account:
                account=core.BankAccount(financial_institution="기업",account_number=account_no,account_name=account_names.get(account_no),currency="KRW",account_type="deposit"); db.session.add(account); db.session.flush()
            dts=[datetime.strptime(r["datetime"],"%Y-%m-%d %H:%M:%S") for r in rows]; start=min(d.date() for d in dts); end=max(d.date() for d in dts)
            old=core.BankTransaction.query.filter(core.BankTransaction.account_id==account.id,core.BankTransaction.transaction_date>=start,core.BankTransaction.transaction_date<=end).all(); replaced=len(old)
            for t in old: db.session.delete(t)
            for i,r in enumerate(rows,start=1):
                dt=datetime.strptime(r["datetime"],"%Y-%m-%d %H:%M:%S"); dep=Decimal(r["deposit"]); wd=Decimal(r["withdrawal"]); bal=Decimal(r["balance"]); h=_sha([account_no,"KRW",dt.date().isoformat(),dt.time().isoformat(),dep,wd,bal,r.get("description","")])
                db.session.add(core.BankTransaction(account_id=account.id,transaction_date=dt.date(),transaction_time=dt.time(),deposit_amount=dep,withdrawal_amount=wd,balance=bal,description=r.get("description"),detail1=r.get("counter_bank"),detail2=r.get("transaction_type"),counterparty=r.get("counterparty"),counter_account=r.get("counter_account"),branch=None,account_subject=None,cms_number=r.get("cms"),financial_institution="기업",source_row_number=i,import_batch_id=correction_batch.id,transaction_hash=h,created_by=actor.id))
                raw={"거래일시":r["datetime"],"출금":r["withdrawal"],"입금":r["deposit"],"거래후 잔액":r["balance"],"거래내용":r.get("description",""),"상대계좌번호":r.get("counter_account",""),"상대은행":r.get("counter_bank",""),"메모":r.get("memo",""),"거래구분":r.get("transaction_type",""),"수표어음금액":r.get("check_amount","0"),"CMS코드":r.get("cms",""),"상대계좌예금주명":r.get("counterparty","")}
                db.session.add(core.ImportRawRow(import_batch_id=correction_batch.id,source_row_number=i,row_status="manual_correction",message=f"사용자 제공 텍스트 기준 거래내역 교체 ({account_no})",raw_data=raw))
            audit_details.append(f"{account_no}:replaced={replaced},new={len(rows)}")
        db.session.add(core.AuditLog(user_id=actor.id,login_id=actor.login_id,action="bank_transactions_corrected_from_text",target_type="import_batch",target_id=str(correction_batch.id),detail="; ".join(audit_details),ip_address="system:data_patch"))

    # Reconciliation snapshot as of 2026-08-31. No opening balance is auto-corrected here.
    cutoff=date(2026,8,31); matched=mismatched=no_data=0; mismatch_accounts=[]
    for ob in core.OpeningBalance.query.filter_by(base_date=date(2026,1,1)).all():
        txs=core.BankTransaction.query.filter(core.BankTransaction.account_id==ob.bank_account_id,core.BankTransaction.transaction_date>=date(2026,1,1),core.BankTransaction.transaction_date<=cutoff).all()
        if not txs: no_data += 1; continue
        latest_date=max(t.transaction_date for t in txs); same=[t for t in txs if t.transaction_date==latest_date]
        reported=(min(same,key=lambda t:t.source_row_number) if ob.currency!="KRW" else max(same,key=lambda t:(t.transaction_time or datetime.min.time(),t.source_row_number))).balance
        calc=Decimal(ob.opening_balance or 0)+sum((Decimal(t.deposit_amount) for t in txs),Decimal(0))-sum((Decimal(t.withdrawal_amount) for t in txs),Decimal(0))
        if calc==Decimal(reported): matched += 1
        else: mismatched += 1; mismatch_accounts.append(ob.account.account_number)
    detail=f"cutoff=2026-08-31 matched={matched} mismatched={mismatched} no_data={no_data} mismatch_accounts={','.join(mismatch_accounts)}"
    existing=core.AuditLog.query.filter_by(action="reconciliation_test_20260831",detail=detail).first()
    if not existing: db.session.add(core.AuditLog(user_id=actor.id,login_id=actor.login_id,action="reconciliation_test_20260831",target_type="opening_balance",target_id="2026-08-31",detail=detail,ip_address="system:data_patch"))
    db.session.commit()
    print("MEDPARK_DATA_PATCH", detail, flush=True)
