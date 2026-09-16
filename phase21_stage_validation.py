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
    if 'phase21_stage_db' in app.view_functions:return
    def dbcheck():
        try:
            cols={r[0] for r in db.session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='fx_rates'")).all()};expected={'unit_amount','rate_type','source_date','source_url','retrieved_at','response_hash','is_active','updated_at'};routes=all(x in app.view_functions for x in ['phase21_fx_lookup','phase21_fx_preview','phase21_fx_confirm']);ok=expected.issubset(cols) and routes;return jsonify({'ok':ok}),200 if ok else 503
        except Exception as e:return jsonify({'ok':False,'error':str(e)[:200]}),503
    def provider():
        try:
            curr=fx.required_currencies() or ['USD','JPY'];unsupported={c for c in curr if c=='CNY'};supported=[c for c in curr if c not in unsupported]
            normal=fx.PROVIDER.lookup(date(2026,5,15),supported) if supported else [];good={r['currency']:r for r in normal if r.get('status')=='lookup_ok'}
            supported_ok=all(c in good for c in supported);jpy_ok=('JPY' not in supported) or ('JPY' in good and Decimal(good['JPY']['unit_amount'])==100)
            weekend=fx.PROVIDER.lookup(date(2026,5,17),supported,False) if supported else [];weekend_ok=all(r.get('status') in ('no_data','error') for r in weekend)
            prev=fx.PROVIDER.lookup(date(2026,5,17),supported,True) if supported else [];pg={r['currency']:r for r in prev if r.get('status')=='lookup_ok'};previous_ok=all(c in pg and pg[c].get('source_date')=='2026-05-15' for c in supported)
            exception_ok=True
            if 'CNY' in unsupported:
                cny=fx.PROVIDER.lookup(date(2026,5,15),['CNY']);exception_ok=all(r.get('status') in ('no_data','error') for r in cny)
            manual_fallback='phase2_fx_rate_add' in app.view_functions
            ok=supported_ok and jpy_ok and weekend_ok and previous_ok and exception_ok and manual_fallback
            return jsonify({'ok':ok,'required':curr,'supported':supported,'manual_exception':sorted(unsupported),'manual_fallback':manual_fallback}),200 if ok else 503
        except Exception as e:return jsonify({'ok':False,'error':str(e)[:200]}),503
    def priority():
        try:
            actor=pkg.core.User.query.filter_by(role='admin',is_active_flag=True).first() or pkg.core.User.query.first();fake=date(2099,1,2);nested=db.session.begin_nested();auto={'currency':'USD','rate':'1400.00','unit_amount':'1','rate_type':RATE_TYPE,'source':SOURCE,'source_date':fake.isoformat(),'source_url':'validation','retrieved_at':pkg.core.utcnow().isoformat(),'response_hash':'0'*64,'status':'lookup_ok'};fx.insert_auto(fake,auto,actor.id);fx.save_manual(fake,'USD',Decimal('1500'),Decimal('1'),'수기등록',actor.id);a=fx.active(fake,'USD');ok=bool(a and a.get('source')==SOURCE and Decimal(str(a.get('rate')))==Decimal('1400'));nested.rollback();db.session.rollback();return jsonify({'ok':ok}),200 if ok else 503
        except Exception as e:db.session.rollback();return jsonify({'ok':False,'error':str(e)[:200]}),503
    def snapshot():
        try:
            v=p2.CashJournalVersion.query.filter_by(status='confirmed').order_by(p2.CashJournalVersion.confirmed_at.desc().nullslast()).first()
            if not v:return jsonify({'ok':True,'note':'no confirmed snapshot'}),200
            before=json.dumps(v.snapshot_json,ensure_ascii=False,sort_keys=True,default=str);p2._snapshot(v.journal_date,pkg.MmtOpeningBalance);db.session.expire_all();after=json.dumps(db.session.get(p2.CashJournalVersion,v.id).snapshot_json,ensure_ascii=False,sort_keys=True,default=str);ok=before==after;return jsonify({'ok':ok}),200 if ok else 503
        except Exception as e:return jsonify({'ok':False,'error':str(e)[:200]}),503
    app.add_url_rule('/phase21-stage/db','phase21_stage_db',dbcheck);app.add_url_rule('/phase21-stage/provider','phase21_stage_provider',provider);app.add_url_rule('/phase21-stage/priority','phase21_stage_priority',priority);app.add_url_rule('/phase21-stage/snapshot','phase21_stage_snapshot',snapshot)
