from decimal import Decimal
from sqlalchemy import text

import app as pkg
import phase2_feature as p2
import phase21_fx_core as fx
from fx_provider_smb import SOURCE, RATE_TYPE


def apply():
    if getattr(fx.save_manual,'_phase21_priority',False):
        return

    def save_manual(day,currency,rate,unit,source,user_id):
        rs=fx.rows(day,currency)
        auto=next((r for r in rs if r.get('source')==SOURCE and (r.get('rate_type') or RATE_TYPE)==RATE_TYPE and r.get('is_active')),None)
        e=next((r for r in rs if r.get('source')==source),None)
        old=dict(e) if e else None
        if e:
            rid=e['id']
            pkg.db.session.execute(text('UPDATE fx_rates SET rate=:r,round_no=1,source=:s WHERE id=CAST(:id AS uuid)'),{'r':rate,'s':source,'id':rid})
        else:
            m=p2.FxRate(base_date=day,currency=currency,rate=rate,round_no=1,source=source,created_by=user_id)
            pkg.db.session.add(m);pkg.db.session.flush();rid=m.id
        row={'unit_amount':str(unit),'rate_type':RATE_TYPE,'source_date':day.isoformat(),'source_url':None,'retrieved_at':pkg.core.utcnow().isoformat(),'response_hash':None}
        fx.extras(rid,row)
        if auto and str(auto['id'])!=str(rid):
            pkg.db.session.execute(text('UPDATE fx_rates SET is_active=FALSE,updated_at=:u WHERE id=CAST(:id AS uuid)'),{'u':pkg.core.utcnow(),'id':str(rid)})
        else:
            fx.deactivate(day,currency,rid)
        return rid,old

    save_manual._phase21_priority=True
    fx.save_manual=save_manual
