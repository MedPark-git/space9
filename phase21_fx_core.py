import io, json, uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from sqlalchemy import text

import app as pkg
import phase2_feature as p2
from fx_provider_smb import SeoulMoneyBrokerageProvider, SOURCE, RATE_TYPE

core=pkg.core; db=pkg.db; app=pkg.app
PROVIDER=SeoulMoneyBrokerageProvider(); ALL=['USD','JPY','EUR','CNY','CNH','HKD','GBP','CHF','AUD','NZD','CAD','SGD','THB','MYR','IDR','PHP','VND']
LEGACY_UNIT_ONE={'USD','EUR','CNY','CNH','HKD','GBP','CHF','AUD','NZD','CAD','SGD','THB','MYR','PHP'}

def migrate():
    before={k:v for k,v in [('fx',p2.FxRate.query.count()),('bt',core.BankTransaction.query.count()),('ob',core.OpeningBalance.query.count()),('ver',p2.CashJournalVersion.query.count()),('batch',core.ImportBatch.query.count()),('raw',core.ImportRawRow.query.count())]}
    stmts=["ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS unit_amount NUMERIC(28,8)","ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS rate_type VARCHAR(50)","ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS source_date DATE","ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS source_url VARCHAR(500)","ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS retrieved_at TIMESTAMPTZ","ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS response_hash VARCHAR(64)","ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE","ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ","CREATE INDEX IF NOT EXISTS ix_fx_rates_lookup21 ON fx_rates(base_date,currency,is_active)"]
    with db.engine.begin() as c:
        for s in stmts:c.execute(text(s))
    after={k:v for k,v in [('fx',p2.FxRate.query.count()),('bt',core.BankTransaction.query.count()),('ob',core.OpeningBalance.query.count()),('ver',p2.CashJournalVersion.query.count()),('batch',core.ImportBatch.query.count()),('raw',core.ImportRawRow.query.count())]}
    if before!=after:raise RuntimeError(f'Phase2.1 additive migration row guard failed: {before}->{after}')

def required_currencies():
    return sorted({r[0].upper() for r in db.session.query(core.BankAccount.currency).filter(core.BankAccount.currency!='KRW',core.BankAccount.is_active.is_(True)).distinct().all() if r[0]})

def rows(day,currency=None):
    q="SELECT id::text,base_date,currency,rate,round_no,source,unit_amount,rate_type,source_date,source_url,retrieved_at,response_hash,is_active,created_at,updated_at FROM fx_rates WHERE base_date=:d";p={'d':day}
    if currency:q+=' AND currency=:c';p['c']=currency
    q+=' ORDER BY currency,is_active DESC,created_at DESC';return [dict(x._mapping) for x in db.session.execute(text(q),p).all()]

def active(day,currency):
    rs=[r for r in rows(day,currency) if r.get('is_active')]
    for r in rs:
        if r.get('source')==SOURCE and (r.get('rate_type') or RATE_TYPE)==RATE_TYPE and r.get('unit_amount'):return r
    for r in rs:
        if r.get('unit_amount') and Decimal(str(r['unit_amount']))>0:return r
    if currency in LEGACY_UNIT_ONE:
        for r in rs:
            if r.get('unit_amount') is None:r=dict(r);r['unit_amount']=Decimal('1');r['compat_legacy']=True;return r
    return None

def effective(day,currency):
    if currency=='KRW':return Decimal('1')
    r=active(day,currency)
    if not r:return None
    unit=Decimal(str(r.get('unit_amount') or 0));rate=Decimal(str(r.get('rate') or 0));return rate/unit if unit>0 and rate>0 else None

def previous_effective(day,currency):
    q="SELECT rate,unit_amount FROM fx_rates WHERE currency=:c AND base_date<:d AND is_active=TRUE ORDER BY base_date DESC,created_at DESC LIMIT 5"
    for x in db.session.execute(text(q),{'c':currency,'d':day}).all():
        r=dict(x._mapping);u=r.get('unit_amount')
        if u is None and currency in LEGACY_UNIT_ONE:u=Decimal('1')
        if u and Decimal(str(u))>0:return Decimal(str(r['rate']))/Decimal(str(u))
    return None

def compare(day,row):
    if row.get('status')!='lookup_ok':return row.get('status','error'),None
    rs=rows(day,row['currency']);same=[r for r in rs if r.get('source')==SOURCE and (r.get('rate_type') or RATE_TYPE)==RATE_TYPE]
    for e in same:
        if e.get('unit_amount') is not None and Decimal(str(e['rate']))==Decimal(row['rate']) and Decimal(str(e['unit_amount']))==Decimal(row['unit_amount']) and str(e.get('source_date'))==row['source_date']:return 'registered',e
    return ('change_review',next((r for r in rs if r.get('is_active')),rs[0])) if rs else ('new',None)

def deactivate(day,currency,keep=None):
    p={'d':day,'c':currency,'u':core.utcnow()};q='UPDATE fx_rates SET is_active=FALSE,updated_at=:u WHERE base_date=:d AND currency=:c AND is_active=TRUE'
    if keep:q+=' AND id<>CAST(:id AS uuid)';p['id']=str(keep)
    db.session.execute(text(q),p)

def extras(rate_id,row):
    db.session.execute(text('UPDATE fx_rates SET unit_amount=:un,rate_type=:rt,source_date=:sd,source_url=:url,retrieved_at=:ra,response_hash=:rh,is_active=TRUE,updated_at=:u WHERE id=CAST(:id AS uuid)'),{'un':Decimal(row['unit_amount']),'rt':row.get('rate_type') or RATE_TYPE,'sd':date.fromisoformat(row['source_date']) if row.get('source_date') else None,'url':row.get('source_url'),'ra':datetime.fromisoformat(row['retrieved_at']) if row.get('retrieved_at') else core.utcnow(),'rh':row.get('response_hash'),'u':core.utcnow(),'id':str(rate_id)})

def insert_auto(day,row,user_id):
    m=p2.FxRate(base_date=day,currency=row['currency'],rate=Decimal(row['rate']),round_no=1,source=SOURCE,created_by=user_id);db.session.add(m);db.session.flush();extras(m.id,row);deactivate(day,row['currency'],m.id);return m.id

def update_auto(existing_id,day,row):
    db.session.execute(text('UPDATE fx_rates SET rate=:r,round_no=1,source=:s WHERE id=CAST(:id AS uuid)'),{'r':Decimal(row['rate']),'s':SOURCE,'id':str(existing_id)});extras(existing_id,row);deactivate(day,row['currency'],existing_id)

def save_manual(day,currency,rate,unit,source,user_id):
    rs=rows(day,currency);e=next((r for r in rs if r.get('source')==source),None);old=dict(e) if e else None
    if e:rid=e['id'];db.session.execute(text('UPDATE fx_rates SET rate=:r,round_no=1,source=:s WHERE id=CAST(:id AS uuid)'),{'r':rate,'s':source,'id':rid})
    else:m=p2.FxRate(base_date=day,currency=currency,rate=rate,round_no=1,source=source,created_by=user_id);db.session.add(m);db.session.flush();rid=m.id
    row={'unit_amount':str(unit),'rate_type':RATE_TYPE,'source_date':day.isoformat(),'source_url':None,'retrieved_at':core.utcnow().isoformat(),'response_hash':None};extras(rid,row);deactivate(day,currency,rid);return rid,old

def rate_info(day):
    out={}
    for c in required_currencies():
        r=active(day,c)
        if r:
            u=Decimal(str(r.get('unit_amount') or 1));out[c]={'rate':str(r['rate']),'unit_amount':str(u),'effective_rate':str(Decimal(str(r['rate']))/u),'rate_type':r.get('rate_type') or RATE_TYPE,'source':r.get('source') or '-','source_date':str(r.get('source_date') or r.get('base_date'))}
    return out

def patch_snapshot():
    if getattr(p2._snapshot,'_phase21',False):return
    original=p2._snapshot
    def snap(day,mmt):
        s=original(day,mmt);info=rate_info(day);fx=Decimal('0');missing=[]
        for a in s.get('accounts',[]):
            if a['currency']=='KRW':continue
            i=info.get(a['currency'])
            if not i:a.update(rate=None,unit_amount=None,krw=None,rate_source=None,rate_source_date=None);missing.append(a['currency']);continue
            bal=Decimal(a['balance']);rate=Decimal(i['rate']);unit=Decimal(i['unit_amount']);krw=bal/unit*rate;fx+=krw;a.update(rate=str(rate),unit_amount=str(unit),krw=str(krw),rate_type=i['rate_type'],rate_source=i['source'],rate_source_date=i['source_date'])
        s['fx_krw_total']=str(fx);s['total_financial']=str(Decimal(s['krw_total'])+fx+Decimal(s['mmt_total']));s['expected']=str(Decimal(s['total_financial'])+Decimal(s['planned_in'])-Decimal(s['planned_out']));s['missing_rates']=sorted(set(missing));s['ready']=s['status']['ready'] and not s['missing_rates'];s['fx_rates_used']=info;return s
    snap._phase21=True;p2._snapshot=snap;p2._rate_for=effective

def preview_path(token):return core.PENDING_DIR/f'{token}_fx21.json'
def save_preview(token,data):core.PENDING_DIR.mkdir(parents=True,exist_ok=True);preview_path(token).write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
def load_preview(token,user_id,is_admin=False):
    p=preview_path(token)
    if not p.exists():return None
    d=json.loads(p.read_text(encoding='utf-8'));return d if d.get('created_by')==str(user_id) or is_admin else None

def self_test(day=date(2026,5,15)):
    rs=PROVIDER.lookup(day,['USD','JPY']);good={r['currency']:r for r in rs if r.get('status')=='lookup_ok'}
    if 'USD' not in good or 'JPY' not in good:raise RuntimeError(f'SMB self-test failed: {rs}')
    if Decimal(good['JPY']['unit_amount'])!=100:raise RuntimeError('JPY unit != 100')
    return rs
