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
        result={'ok':False}
        try:
            cols={r[0] for r in db.session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='fx_rates'")).all()};expected={'unit_amount','rate_type','source_date','source_url','retrieved_at','response_hash','is_active','updated_at'}
            required=fx.required_currencies() or ['USD','JPY'];normal=fx.PROVIDER.lookup(date(2026,5,15),required);good={r['currency']:r for r in normal if r.get('status')=='lookup_ok'};required_ok=all(c in good for c in required)
            jpy_ok=('JPY' not in required) or ('JPY' in good and Decimal(good['JPY']['unit_amount'])==100)
            weekend=fx.PROVIDER.lookup(date(2026,5,17),required,False);weekend_ok=all(r.get('status') in ('no_data','error') for r in weekend)
            prev=fx.PROVIDER.lookup(date(2026,5,17),required,True);pg={r['currency']:r for r in prev if r.get('status')=='lookup_ok'};previous_ok=all(c in pg and pg[c].get('source_date')=='2026-05-15' for c in required)
            formula_ok=True if 'JPY' not in good else (Decimal('1000000')/Decimal(good['JPY']['unit_amount'])*Decimal(good['JPY']['rate']))>0
            endpoints=all(x in app.view_functions for x in ['phase21_fx_lookup','phase21_fx_preview','phase21_fx_confirm'])
            priority_ok=False;actor=pkg.core.User.query.filter_by(role='admin',is_active_flag=True).first() or pkg.core.User.query.first()
            if actor:
                nested=db.session.begin_nested()
                try:
                    fake=date(2099,1,2);auto={'currency':'USD','rate':'1400.00','unit_amount':'1','rate_type':RATE_TYPE,'source':SOURCE,'source_date':fake.isoformat(),'source_url':'validation','retrieved_at':pkg.core.utcnow().isoformat(),'response_hash':'0'*64,'status':'lookup_ok'};fx.insert_auto(fake,auto,actor.id);fx.save_manual(fake,'USD',Decimal('1500'),Decimal('1'),'수기등록',actor.id);a=fx.active(fake,'USD');priority_ok=bool(a and a.get('source')==SOURCE and Decimal(str(a.get('rate')))==Decimal('1400.00'))
                finally:nested.rollback();db.session.rollback()
            ver=p2.CashJournalVersion.query.filter_by(status='confirmed').order_by(p2.CashJournalVersion.confirmed_at.desc().nullslast()).first();snapshot_ok=True
            if ver:
                before=json.dumps(ver.snapshot_json,ensure_ascii=False,sort_keys=True,default=str);p2._snapshot(ver.journal_date,pkg.MmtOpeningBalance);db.session.expire_all();again=db.session.get(p2.CashJournalVersion,ver.id);after=json.dumps(again.snapshot_json,ensure_ascii=False,sort_keys=True,default=str);snapshot_ok=before==after
            result={'columns_ok':expected.issubset(cols),'routes_ok':endpoints,'required_currencies':required,'provider_rows':normal,'required_lookup_ok':required_ok,'jpy_unit_ok':jpy_ok,'weekend_no_data':weekend_ok,'previous_source_date_ok':previous_ok,'jpy_formula_ok':formula_ok,'source_priority_ok':priority_ok,'snapshot_immutable_ok':snapshot_ok};result['ok']=all(result[k] for k in ['columns_ok','routes_ok','required_lookup_ok','jpy_unit_ok','weekend_no_data','previous_source_date_ok','jpy_formula_ok','source_priority_ok','snapshot_immutable_ok'])
        except Exception as e:
            db.session.rollback();result={'ok':False,'error':str(e)[:500]}
        return jsonify(result),200
    app.add_url_rule('/phase21-validation','phase21_validation',view,methods=['GET'])
