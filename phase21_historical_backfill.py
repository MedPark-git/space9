import calendar
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

import app as pkg
import phase2_feature as p2
import phase21_fx_core as fx
from fx_provider_smb import SOURCE, RATE_TYPE

core = pkg.core
db = pkg.db
app = pkg.app

LOCK_KEY = 874221946
START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 8, 31)
UNSUPPORTED_AUTO = {"CNY"}


def _audit(actor, action, target_id, detail):
    db.session.add(core.AuditLog(user_id=actor.id, login_id=actor.login_id, action=action, target_type="fx_rate", target_id=str(target_id), detail=str(detail)[:500], ip_address="system:fx_backfill"))


def _version_hashes():
    out = {}
    for v in p2.CashJournalVersion.query.all():
        raw = json.dumps(v.snapshot_json, ensure_ascii=False, sort_keys=True, default=str)
        out[str(v.id)] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return out


def _guards():
    return {"bank_transactions": core.BankTransaction.query.count(), "opening_balances": core.OpeningBalance.query.count(), "import_batches": core.ImportBatch.query.count(), "import_raw_rows": core.ImportRawRow.query.count(), "cash_journal_versions": p2.CashJournalVersion.query.count(), "version_hashes": _version_hashes()}


def _auto_row(day, currency):
    candidates = [r for r in fx.rows(day, currency) if r.get("source") == SOURCE and (r.get("rate_type") or RATE_TYPE) == RATE_TYPE and r.get("unit_amount") is not None and Decimal(str(r.get("unit_amount") or 0)) > 0 and Decimal(str(r.get("rate") or 0)) > 0]
    return candidates[0] if candidates else None


def _activate_existing(row, day, currency):
    db.session.execute(text("UPDATE fx_rates SET is_active=TRUE, updated_at=:u WHERE id=CAST(:id AS uuid)"), {"u": core.utcnow(), "id": str(row["id"])})
    fx.deactivate(day, currency, row["id"])


def _clone_previous(day, currency):
    prev = fx.active(day - timedelta(days=1), currency)
    if not prev or prev.get("source") != SOURCE or not prev.get("unit_amount"):
        return None
    return {"status": "lookup_ok", "currency": currency, "rate": str(prev["rate"]), "unit_amount": str(prev["unit_amount"]), "rate_type": prev.get("rate_type") or RATE_TYPE, "source_date": str(prev.get("source_date") or prev.get("base_date")), "source_url": prev.get("source_url"), "retrieved_at": core.utcnow().isoformat(), "response_hash": prev.get("response_hash")}


def _previous_lookup(day, currency):
    rows = fx.PROVIDER.lookup(day, [currency], True)
    return next((r for r in rows if r.get("currency") == currency and r.get("status") == "lookup_ok"), None)


def _save_auto(day, row, user_id):
    existing = _auto_row(day, row["currency"])
    if existing:
        same = Decimal(str(existing["rate"])) == Decimal(str(row["rate"])) and Decimal(str(existing["unit_amount"])) == Decimal(str(row["unit_amount"])) and str(existing.get("source_date") or existing.get("base_date")) == str(row.get("source_date"))
        if same:
            _activate_existing(existing, day, row["currency"])
            return "existing"
        fx.update_auto(existing["id"], day, row)
        return "updated"
    fx.insert_auto(day, row, user_id)
    return "inserted"


def _month_bounds(year, month):
    return max(START_DATE, date(year, month, 1)), min(END_DATE, date(year, month, calendar.monthrange(year, month)[1]))


def _next_month():
    for month in range(1, 9):
        marker = f"2026-{month:02d}"
        if not core.AuditLog.query.filter_by(action="fx_rate_historical_backfill_month", target_id=marker).first():
            return 2026, month
    return None


def _process_month(actor, year, month):
    before = _guards()
    required = fx.required_currencies()
    supported = [c for c in required if c not in UNSUPPORTED_AUTO]
    unsupported = [c for c in required if c in UNSUPPORTED_AUTO]
    start, end = _month_bounds(year, month)
    stats = {"inserted": 0, "updated": 0, "existing": 0, "carry": 0, "days": 0}
    day = start
    while day <= end:
        missing = []
        for currency in supported:
            existing = _auto_row(day, currency)
            if existing:
                _activate_existing(existing, day, currency)
                stats["existing"] += 1
            else:
                missing.append(currency)
        if missing:
            fetched = {}
            if day.weekday() < 5:
                rows = fx.PROVIDER.lookup(day, missing, False)
                for row in rows:
                    fetched[row.get("currency")] = row
            for currency in missing:
                row = fetched.get(currency)
                if row and row.get("status") == "lookup_ok":
                    pass
                elif row and row.get("status") not in ("no_data", "not_found", None):
                    raise RuntimeError(f"FX backfill source error {day} {currency}: {row}")
                else:
                    row = _clone_previous(day, currency)
                    if row:
                        stats["carry"] += 1
                    else:
                        row = _previous_lookup(day, currency)
                        if not row:
                            raise RuntimeError(f"No Seoul Money Brokerage rate available for {day} {currency}")
                        stats["carry"] += 1
                stats[_save_auto(day, row, actor.id)] += 1
        day_marker = day.isoformat()
        if not core.AuditLog.query.filter_by(action="fx_rate_historical_backfill_day", target_id=day_marker).first():
            source_dates = {}
            for currency in supported:
                ar = _auto_row(day, currency)
                if ar:
                    source_dates[currency] = str(ar.get("source_date") or ar.get("base_date"))
            _audit(actor, "fx_rate_historical_backfill_day", day_marker, f"source={SOURCE} currencies={','.join(supported)} source_dates={json.dumps(source_dates, ensure_ascii=False, separators=(',',':'))}")
        db.session.commit()
        stats["days"] += 1
        day += timedelta(days=1)
    after = _guards()
    if before != after:
        raise RuntimeError(f"Production data guard failed during FX backfill {year}-{month:02d}: {before} -> {after}")
    marker = f"{year}-{month:02d}"
    _audit(actor, "fx_rate_historical_backfill_month", marker, f"range={start}~{end} supported={','.join(supported)} unsupported_manual={','.join(unsupported)} stats={json.dumps(stats, separators=(',',':'))}")
    _audit(actor, "fx_rate_historical_backfill_worker", marker, f"completed={marker}")
    db.session.commit()
    print("FX_BACKFILL_MONTH_COMPLETED", marker, stats, "unsupported", unsupported, flush=True)


def register():
    with app.app_context():
        db.session.execute(text(f"SELECT pg_advisory_lock({LOCK_KEY})"))
        try:
            recent = core.AuditLog.query.filter(core.AuditLog.action == "fx_rate_historical_backfill_worker", core.AuditLog.created_at >= datetime.now(timezone.utc) - timedelta(minutes=5)).first()
            if recent:
                db.session.rollback()
                return
            target = _next_month()
            if not target:
                db.session.rollback()
                print("FX_BACKFILL_ALREADY_COMPLETE", flush=True)
                return
            actor = core.User.query.filter_by(role="admin", is_active_flag=True).order_by(core.User.created_at).first() or core.User.query.first()
            if not actor:
                raise RuntimeError("No active user available for FX historical backfill audit")
            _process_month(actor, target[0], target[1])
        finally:
            try:
                db.session.execute(text(f"SELECT pg_advisory_unlock({LOCK_KEY})"))
                db.session.commit()
            except Exception:
                db.session.rollback()
