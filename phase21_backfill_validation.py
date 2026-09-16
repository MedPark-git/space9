from datetime import date, timedelta
from decimal import Decimal

from flask import Response

import app as pkg
import phase21_fx_core as fx
from fx_provider_smb import SOURCE, RATE_TYPE

core = pkg.core
db = pkg.db
app = pkg.app
START = date(2026,1,1)
END = date(2026,8,31)
UNSUPPORTED_AUTO = {"CNY"}
REGISTERED = False


def _auto(day,currency):
    for r in fx.rows(day,currency):
        if r.get('source')==SOURCE and (r.get('rate_type') or RATE_TYPE)==RATE_TYPE and r.get('is_active') and r.get('unit_amount') is not None:
            try:
                if Decimal(str(r['rate']))>0 and Decimal(str(r['unit_amount']))>0:
                    return r
            except Exception:
                pass
    return None


def _validate():
    required = fx.required_currencies()
    supported = [c for c in required if c not in UNSUPPORTED_AUTO]
    missing=[]; bad_source_date=[]; bad_jpy=[]; days=0
    d=START
    while d<=END:
        days+=1
        for c in supported:
            r=_auto(d,c)
            if not r:
                missing.append(f'{d}:{c}')
                continue
            sd=r.get('source_date') or r.get('base_date')
            if sd and str(sd)>d.isoformat(): bad_source_date.append(f'{d}:{c}:{sd}')
            if c=='JPY' and Decimal(str(r['unit_amount']))!=Decimal('100'): bad_jpy.append(f'{d}:{r["unit_amount"]}')
        d+=timedelta(days=1)

    tx_missing=[]; tx_checked=0; tx_supported_missing=[]; tx_cny_missing=[]
    q=(db.session.query(core.BankTransaction.transaction_date, core.BankAccount.currency)
       .join(core.BankAccount, core.BankTransaction.account_id==core.BankAccount.id)
       .filter(core.BankTransaction.transaction_date>=START,
               core.BankTransaction.transaction_date<=END,
               core.BankAccount.currency!='KRW')
       .distinct())
    for tx_date,curr in q.all():
        tx_checked+=1
        if fx.effective(tx_date,curr) is None:
            item=f'{tx_date}:{curr}'; tx_missing.append(item)
            if curr in UNSUPPORTED_AUTO: tx_cny_missing.append(item)
            else: tx_supported_missing.append(item)

    cny_auto = 0
    for r in db.session.execute(pkg.core.text("SELECT COUNT(*) FROM fx_rates WHERE base_date BETWEEN :s AND :e AND currency='CNY' AND source=:src"), {'s':START,'e':END,'src':SOURCE}).all():
        cny_auto=int(r[0])

    ok=(not missing and not bad_source_date and not bad_jpy and not tx_supported_missing and cny_auto==0)
    return {
        'ok':ok,'days':days,'required':required,'supported':supported,
        'expected_supported_rows':days*len(supported),'missing_count':len(missing),
        'bad_source_date_count':len(bad_source_date),'bad_jpy_count':len(bad_jpy),
        'fx_transaction_date_currency_pairs':tx_checked,
        'fx_transaction_missing_rate_count':len(tx_missing),
        'supported_transaction_missing_count':len(tx_supported_missing),
        'cny_transaction_missing_count':len(tx_cny_missing),
        'cny_auto_rows':cny_auto,
        'missing_sample':missing[:10],
        'tx_missing_sample':tx_missing[:20],
    }


def register():
    global REGISTERED
    if REGISTERED:return
    def view():
        r=_validate()
        text=(f"ok={str(r['ok']).lower()} days={r['days']} required={','.join(r['required'])} supported={','.join(r['supported'])} "
              f"expected={r['expected_supported_rows']} missing={r['missing_count']} bad_source_date={r['bad_source_date_count']} bad_jpy={r['bad_jpy_count']} "
              f"tx_pairs={r['fx_transaction_date_currency_pairs']} tx_missing={r['fx_transaction_missing_rate_count']} supported_tx_missing={r['supported_transaction_missing_count']} "
              f"cny_tx_missing={r['cny_transaction_missing_count']} cny_auto={r['cny_auto_rows']} tx_missing_sample={';'.join(r['tx_missing_sample'])}")
        return Response(text,mimetype='text/plain')
    app.add_url_rule('/phase21-backfill-validation','phase21_backfill_validation',view,methods=['GET'])
    REGISTERED=True
