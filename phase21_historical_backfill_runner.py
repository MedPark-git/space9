from sqlalchemy import text

import phase21_historical_backfill as backfill


def register():
    with backfill.app.app_context():
        backfill.db.session.execute(text(f"SELECT pg_advisory_lock({backfill.LOCK_KEY})"))
        try:
            target = backfill._next_month()
            if not target:
                backfill.db.session.rollback()
                print("FX_BACKFILL_ALREADY_COMPLETE", flush=True)
                return
            actor = backfill.core.User.query.filter_by(role="admin", is_active_flag=True).order_by(backfill.core.User.created_at).first() or backfill.core.User.query.first()
            if not actor:
                raise RuntimeError("No active user available for FX historical backfill audit")
            backfill._process_month(actor, target[0], target[1])
        finally:
            try:
                backfill.db.session.execute(text(f"SELECT pg_advisory_unlock({backfill.LOCK_KEY})"))
                backfill.db.session.commit()
            except Exception:
                backfill.db.session.rollback()
