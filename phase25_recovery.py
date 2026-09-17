import base64, hashlib, lzma, os
from pathlib import Path
from flask import Response, abort, request
from sqlalchemy import func
import app as pkg
import phase25_core as p25
app=pkg.app;core=pkg.core;db=pkg.db
ROOT=Path('/app/user_data/phase25_recovery');REGISTERED=False
FILES={
 'krw':{'chunks':5,'prefix':'PHASE25_KRW_','name':'거래내역조회Ⅱ 20260914 09-42-38_원화계좌.xls','rows':4580,'review':0,'sha':'6b1d0af2763bc424ecee226db64afdca3a7ac6df8a0947c0f4a2f01865df32b5'},
 'fx':{'chunks':1,'prefix':'PHASE25_FX_','name':'거래내역조회Ⅱ 20260914 09-43-16_외화계좌.xls','rows':256,'review':5,'sha':'e0016a7799eb1e319da1c4be730e22f9fefde158eb46dc6cd244db965ecb4dc5'}}

def _auth():
    if not os.getenv('PHASE25_RECOVERY_TOKEN') or request.args.get('token')!=os.getenv('PHASE25_RECOVERY_TOKEN'):abort(404)

def _actor():
    u=core.User.query.filter(func.lower(core.User.login_id)=='admin',core.User.is_active_flag.is_(True)).first()
    if not u:raise RuntimeError('active admin user not found')
    return u

def _materialize(kind):
    c=FILES[kind];enc=''.join(os.getenv(c['prefix']+str(i),'') for i in range(c['chunks']))
    if not enc:raise RuntimeError(f'{kind} payload missing')
    raw=lzma.decompress(base64.b64decode(enc));
    if hashlib.sha256(raw).hexdigest()!=c['sha']:raise RuntimeError(f'{kind} sha mismatch')
    ROOT.mkdir(parents=True,exist_ok=True);p=ROOT/f'{kind}.xls';p.write_bytes(raw);return p

def _parsed(kind):
    path=_materialize(kind);p=core.parse_file(path,kind);dups=core.existing_hashes(kind,[r['transaction_hash'] for r in p['rows']]);keys=set();unmatched=[]
    for r in p['rows']:
        k=(r['bank'],r['account_number'],r['currency'])
        if k in keys:continue
        keys.add(k)
        if not core.BankAccount.query.filter_by(financial_institution=k[0],account_number=k[1],currency=k[2]).first():unmatched.append('|'.join(k))
    return path,p,dups,unmatched

def _preview(kind):
    _,p,d,u=_parsed(kind);new=sum(r['transaction_hash'] not in d for r in p['rows']);return f"type={kind} period={p['query_start_date']}~{p['query_end_date']} rows={len(p['rows'])} new={new} duplicate={len(p['rows'])-new} review={len(p['reviews'])} error={len(p['errors'])} unmatched={len(u)}"

def _confirm(kind,duplicate=False):
    path,p,dups,unmatched=_parsed(kind);c=FILES[kind]
    if unmatched:raise RuntimeError('canonical account unmatched '+','.join(unmatched))
    if len(p['rows'])!=c['rows'] or len(p['reviews'])!=c['review'] or p['errors']:raise RuntimeError('source validation failed')
    new=sum(r['transaction_hash'] not in dups for r in p['rows'])
    if duplicate:
        if new!=0:raise RuntimeError(f'duplicate test expected new=0 actual={new}')
    else:
        if core.ImportBatch.query.filter_by(file_type=kind,status='confirmed').count():raise RuntimeError(f'{kind} base batch already exists')
        if new!=c['rows']:raise RuntimeError(f'{kind} expected new={c["rows"]} actual={new}')
    actor=_actor();code=core.next_batch_code();stored=core.IMPORT_DIR/f'{code}.xls';stored.parent.mkdir(parents=True,exist_ok=True);stored.write_bytes(path.read_bytes());batch=core.ImportBatch(batch_code=code,file_type=kind,original_filename=c['name'],stored_path=str(stored),file_sha256=core.file_sha256(path),structure_hash=p['structure_hash'],query_start_date=core.parse_iso_date(p['query_start_date']),query_end_date=core.parse_iso_date(p['query_end_date']),total_rows=len(p['rows'])+len(p['reviews'])+len(p['errors']),new_rows=new,duplicate_rows=len(p['rows'])-new,review_rows=len(p['reviews']),error_rows=len(p['errors']),status='confirmed',created_by=actor.id,confirmed_at=core.utcnow());db.session.add(batch);db.session.flush()
    try:
        for r in p['rows']:
            dup=r['transaction_hash'] in dups;db.session.add(core.ImportRawRow(import_batch_id=batch.id,source_row_number=r['source_row_number'],row_status='duplicate' if dup else 'new',raw_data=r['raw']))
            if dup:continue
            a=core.BankAccount.query.filter_by(financial_institution=r['bank'],account_number=r['account_number'],currency=r['currency']).first()
            if not a:raise RuntimeError('unexpected new account')
            db.session.add(core.BankTransaction(account_id=a.id,transaction_date=core.parse_iso_date(r['transaction_date']),transaction_time=core.parse_iso_time(r.get('transaction_time')),deposit_amount=core.dec(r['deposit']),withdrawal_amount=core.dec(r['withdrawal']),balance=core.dec(r['balance']),description=r.get('description'),detail1=r.get('detail1'),detail2=r.get('detail2'),counterparty=r.get('counterparty'),counter_account=r.get('counter_account'),branch=r.get('branch'),account_subject=r.get('account_subject'),cms_number=r.get('cms_number'),financial_institution=r['bank'],source_row_number=r['source_row_number'],import_batch_id=batch.id,transaction_hash=r['transaction_hash'],created_by=actor.id))
        for r in p['reviews']:db.session.add(core.ImportRawRow(import_batch_id=batch.id,source_row_number=r['source_row_number'],row_status='review',message=r['message'],raw_data=r['raw']))
        for r in p['errors']:db.session.add(core.ImportRawRow(import_batch_id=batch.id,source_row_number=r['source_row_number'],row_status='error',message=r['message'],raw_data=r['raw']))
        db.session.flush();post=p25.post_import_for_batch(batch.id,actor);core.audit('phase25_recovery_import_confirmed',user=actor,target_type='import_batch',target_id=batch.id,detail=f'{code} {kind} new={batch.new_rows} duplicate={batch.duplicate_rows} review={batch.review_rows} error={batch.error_rows}');db.session.commit()
    except Exception:db.session.rollback();stored.unlink(missing_ok=True);raise
    actual=core.BankTransaction.query.filter_by(import_batch_id=batch.id).count();return f'batch={code} type={kind} new={batch.new_rows} duplicate={batch.duplicate_rows} review={batch.review_rows} actual_tx={actual} total_bank_tx={core.BankTransaction.query.count()} post={post}'

def _finalize():
    import phase2_feature as p2
    actor=_actor();ids=[x[0] for x in db.session.query(core.BankTransaction.id).all()];created=0
    for i in range(0,len(ids),1000):created+=p25.ensure_annotations_for_ids(ids[i:i+1000])
    latest=db.session.query(func.max(core.BankTransaction.transaction_date)).scalar();recalc=p25.recalc_all_accounts(latest);core.audit('phase25_recovery_core_recalculated',user=actor,target_type='daily_balance',target_id=str(latest),detail=f'tx={len(ids)} annotations_created={created} daily_rows={recalc}');db.session.commit();return f'tx={len(ids)} annotations={p2.TransactionAnnotation.query.count()} daily={p2.DailyBalanceCheck.query.count()} latest={latest} recalculated={recalc}'

def _verify():
    import phase2_feature as p2
    tx=core.BankTransaction.query.count();krw=core.BankTransaction.query.join(core.BankAccount).filter(core.BankAccount.currency=='KRW').count();mn,mx=db.session.query(func.min(core.BankTransaction.transaction_date),func.max(core.BankTransaction.transaction_date)).one();bs=[]
    for b in core.ImportBatch.query.filter(core.ImportBatch.file_type.in_(['krw','fx'])).order_by(core.ImportBatch.created_at):bs.append(f'{b.batch_code}:{b.file_type}:new={b.new_rows}:dup={b.duplicate_rows}:review={b.review_rows}:actual={core.BankTransaction.query.filter_by(import_batch_id=b.id).count()}')
    return f'bank_accounts={core.BankAccount.query.count()} opening={core.OpeningBalance.query.count()} mmt={core.MmtTransaction.query.count()} tx={tx} krw={krw} fx={tx-krw} period={mn}~{mx} annotations={p2.TransactionAnnotation.query.count()} daily={p2.DailyBalanceCheck.query.count()} batches={"|".join(bs)}'

def register():
    global REGISTERED
    if REGISTERED:return
    def view():
        _auth();a=request.args.get('action') or ''
        try:
            if a=='preview_krw':out=_preview('krw')
            elif a=='preview_fx':out=_preview('fx')
            elif a=='confirm_krw':out=_confirm('krw')
            elif a=='confirm_fx':out=_confirm('fx')
            elif a=='duplicate_krw':out=_confirm('krw',True)
            elif a=='duplicate_fx':out=_confirm('fx',True)
            elif a=='finalize_core':out=_finalize()
            elif a=='verify':out=_verify()
            else:abort(404)
            return Response('OK '+out,mimetype='text/plain')
        except Exception as e:db.session.rollback();return Response('ERROR '+str(e),status=500,mimetype='text/plain')
    app.add_url_rule('/maintenance/phase25-recovery','phase25_recovery',view,methods=['GET']);REGISTERED=True
