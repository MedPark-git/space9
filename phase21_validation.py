from datetime import date
from decimal import Decimal
from flask import jsonify
from sqlalchemy import text

import app as pkg
import phase21_fx_core as fx


def register():
    app=pkg.app;db=pkg.db
    if 'phase21_validation' in app.view_functions:return
    def view():
        result={'ok':False}
        try:
            cols={r[0] for r in db.session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='fx_rates'")).all()}
            expected={'unit_amount','rate_type','source_date','source_url','retrieved_at','response_hash','is_active','updated_at'}
            required=fx.required_currencies()
            normal=fx.PROVIDER.lookup(date(2026,5,15),['USD','JPY'])
            good={r['currency']:r for r in normal if r.get('status')=='lookup_ok'}
            weekend=fx.PROVIDER.lookup(date(2026,5,17),['USD','JPY'],False)
            previous=fx.PROVIDER.lookup(date(2026,5,17),['USD','JPY'],True)
            pg={r['currency']:r for r in previous if r.get('status')=='lookup_ok'}
            endpoints=all(x in app.view_functions for x in ['phase21_fx_lookup','phase21_fx_preview','phase21_fx_confirm'])
            jpy=good.get('JPY');usd=good.get('USD')
            calc_ok=False
            if jpy:
                calc=Decimal('1000000')/Decimal(jpy['unit_amount'])*Decimal(jpy['rate'])
                calc_ok=calc>0 and Decimal(jpy['unit_amount'])==Decimal('100')
            no_weekend=all(r.get('status') in ('no_data','error') for r in weekend)
            previous_ok=bool(pg) and all(r.get('source_date')=='2026-05-15' for r in pg.values())
            result={'ok':expected.issubset(cols) and endpoints and bool(usd) and bool(jpy) and calc_ok and no_weekend and previous_ok,'columns_ok':expected.issubset(cols),'routes_ok':endpoints,'required_currencies':required,'usd':{'rate':usd.get('rate'),'unit':usd.get('unit_amount'),'source_date':usd.get('source_date')} if usd else None,'jpy':{'rate':jpy.get('rate'),'unit':jpy.get('unit_amount'),'source_date':jpy.get('source_date')} if jpy else None,'weekend_no_data':no_weekend,'previous_source_date_ok':previous_ok,'jpy_formula_ok':calc_ok}
            return jsonify(result),200 if result['ok'] else 503
        except Exception as e:
            result['error']=str(e)[:500];return jsonify(result),503
    app.add_url_rule('/phase21-validation','phase21_validation',view,methods=['GET'])
