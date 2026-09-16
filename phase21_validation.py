import json
from datetime import date
from decimal import Decimal
from flask import jsonify
from sqlalchemy import text

import app as pkg
import phase2_feature as p2
import phase21_fx_core as fx
from fx_provider_smb import SOURCE, RATE_TYPE


def register():
    app=pkg.app;db=pkg.db
    if 'phase21_validation' in app.view_functions:return
    def view():
        try:
            cols={r[0] for r in db.session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='fx_rates'")).all()}
            expected={'unit_amount','rate_type','source_date','source_url','retrieved_at','response_hash','is_active','updated_at'}
            required=fx.required_currencies()
            test_curr=required or ['USD','JPY']
            normal=fx.PROVIDER.lookup(date(2026,5,15),test_curr)
            good={r['currency']:r for r in normal if r.get('status')=='lookup_ok'}
            required_ok=all(c in good for c in test_curr)
            jpy_ok=True
            if 'JPY' in test_curr:jpy_ok='JPY' in good and Decimal(good['JPY']['unit_amount'])==Decimal('100')
            weekend=fx.PROVIDER.lookup(date(2026,5,17),test_curr,False)
            weekend_ok=all(r.get('status') in ('no_data','error') for r in weekend)
            prev=fx.PROVIDER.lookup(date(2026,5,17),test_curr,True)
            pg={r['currency']:r for r in prev if r.get('status')=='lookup_ok'}
            previous_ok=all(c in pg and pg[c].get('source_date')=='2026-05-15' for c in test_curr)
            formula_ok=True
            if 'JPY' in good:
                formula_ok=(Decimal('1000000')/Decimal(good['JPY']['unit_amount'])*Decimal(good['JPY']['rate']))>0
            endpoints=all(x in app.view_functions for x in ['phase21_fx_lookup','phase21_fx_preview','phase21_fx_confirm'])
            # Source priority test in a nested transaction; all writes are rolled back.
            actor=pkg.core.User.query.filter_by(role='admin',is_active_flag=True).first() or pkg.core.User.query.first()
            priority_ok=False
            if actor:
                fake=date(2099,1,2);nested=db.session.begin_nested()
                try:
                    auto={'currency':'USD','rate':'1400.00','unit_amount':'1','rate_type':RATE_TYPE,'source':SOURCE,'source_date':fake.isoformat(),'source_url':'validation','retrieved_at':pkg.core.utcnow().isoformat(),'response_hash':'0'*64,'status':'lookup_ok'}
                    fx.insert_auto(fake,auto,actor.id)
                    fx.save_manual(fake,'USD',Decimal('1500'),Decimal('1'),'수기등록',actor.id)
                    active=fx.active(fake,'USD');priority_ok=bool(active and active.get('source')==SOURCE and Decimal(str(active.get('rate')))==Decimal('1400.00'))
                finally:
                    nested.rollback();db.session.rollback()
            # Existing Snapshot must remain byte-for-byte unchanged after live calculation.
            ver=p2.CashJournalVersion.query.filter_by(status='confirmed').order_by(p2.CashJournalVersion.confirmed_at.desc().nullslast()).first()
            snapshot_ok=True
            if ver:
                before=json.dumps(ver.snapshot_json,ensure_ascii=False,sort_keys=True,default=str)
                p2._snapshot(ver.journal_date,pkg.MmtOpeningBalance)
                db.session.expire_all();again=db.session.get(p2.CashJournalVersion,ver.id)
                after=json.dumps(again.snapshot_json,ensure_ascii=False,sort_keys=True,default=str)
                snapshot_ok=before==after
            result={'ok':expected.issubset(cols) and endpoints and required_ok and jpy_ok and weekend_ok and previous_ok and formula_ok and priority_ok and snapshot_ok,'columns_ok':expected.issubset(cols),'routes_ok':endpoints,'required_currencies':test_curr,'required_lookup_ok':required_ok,'jpy_unit_ok':jpy_ok,'weekend_no_data':weekend_ok,'previous_source_date_ok':previous_ok,'jpy_formula_ok':formula_ok,'source_priority_ok':priority_ok,'snapshot_immutable_ok':snapshot_ok}
            return jsonify(result),200 if result['ok'] else 503
        except Exception as e:
            db.session.rollback();return jsonify({'ok':False,'error':str(e)[:500]}),503
    app.add_url_rule('/phase21-validation','phase21_validation',view,methods=['GET'])
