from datetime import datetime, time, date
from decimal import Decimal

from flask import render_template, request
from flask_login import login_required

_registered = False
BASE_DATE = date(2026, 1, 1)
DEFAULT_CUTOFF = date(2026, 8, 31)


def _select_bank_reported(txs, currency):
    if not txs:
        return None
    latest_date = max(t.transaction_date for t in txs)
    same = [t for t in txs if t.transaction_date == latest_date]
    if currency != "KRW":
        return min(same, key=lambda t: t.source_row_number)
    return max(same, key=lambda t: (t.transaction_time or time.min, t.source_row_number))


def _select_mmt_reported(txs):
    if not txs:
        return None
    latest_date = max(t.transaction_date for t in txs)
    same = [t for t in txs if t.transaction_date == latest_date]
    return max(same, key=lambda t: (t.transaction_time or time.min, t.source_row_number))


def register():
    global _registered
    if _registered:
        return
    import app as pkg
    core = pkg.core
    app = pkg.app

    @login_required
    def opening_balance_reconcile():
        cutoff_text = (request.args.get("cutoff") or DEFAULT_CUTOFF.isoformat()).strip()
        try:
            cutoff = datetime.strptime(cutoff_text, "%Y-%m-%d").date()
        except ValueError:
            cutoff = DEFAULT_CUTOFF
        results, totals = [], {}

        bank_obs = core.OpeningBalance.query.filter_by(base_date=BASE_DATE).join(core.BankAccount).order_by(
            core.BankAccount.financial_institution, core.BankAccount.account_number, core.BankAccount.currency
        ).all()
        for ob in bank_obs:
            txs = core.BankTransaction.query.filter(
                core.BankTransaction.account_id == ob.bank_account_id,
                core.BankTransaction.transaction_date >= BASE_DATE,
                core.BankTransaction.transaction_date <= cutoff,
            ).all()
            opening = Decimal(ob.opening_balance or 0)
            deposits = sum((Decimal(t.deposit_amount) for t in txs), Decimal("0"))
            withdrawals = sum((Decimal(t.withdrawal_amount) for t in txs), Decimal("0"))
            calculated = opening + deposits - withdrawals
            reported_tx = _select_bank_reported(txs, ob.currency)
            reported = Decimal(reported_tx.balance) if reported_tx else None
            difference = calculated - reported if reported is not None else None
            results.append({
                "kind": "예금", "bank": ob.account.financial_institution, "account": ob.account.account_number,
                "account_name": ob.account.account_name, "currency": ob.currency, "opening": opening,
                "deposits": deposits, "withdrawals": withdrawals, "calculated": calculated,
                "reported": reported, "difference": difference,
                "reported_date": reported_tx.transaction_date if reported_tx else None,
                "match": difference == 0 if difference is not None else False,
            })

        for ob in pkg.MmtOpeningBalance.query.filter_by(base_date=BASE_DATE).order_by(
            pkg.MmtOpeningBalance.financial_institution, pkg.MmtOpeningBalance.account_number
        ).all():
            txs = core.MmtTransaction.query.filter(
                core.MmtTransaction.financial_institution == ob.financial_institution,
                core.MmtTransaction.account_number == ob.account_number,
                core.MmtTransaction.currency == ob.currency,
                core.MmtTransaction.transaction_date >= BASE_DATE,
                core.MmtTransaction.transaction_date <= cutoff,
            ).all()
            opening = Decimal(ob.opening_balance or 0)
            deposits = sum((Decimal(t.deposit_amount) for t in txs), Decimal("0"))
            withdrawals = sum((Decimal(t.withdrawal_amount) for t in txs), Decimal("0"))
            calculated = opening + deposits - withdrawals
            reported_tx = _select_mmt_reported(txs)
            reported = Decimal(reported_tx.balance) if reported_tx else None
            difference = calculated - reported if reported is not None else None
            results.append({
                "kind": "MMT", "bank": ob.financial_institution, "account": ob.account_number,
                "account_name": ob.account_name, "currency": ob.currency, "opening": opening,
                "deposits": deposits, "withdrawals": withdrawals, "calculated": calculated,
                "reported": reported, "difference": difference,
                "reported_date": reported_tx.transaction_date if reported_tx else None,
                "match": difference == 0 if difference is not None else False,
            })

        for r in results:
            t = totals.setdefault(r["currency"], {
                "opening": Decimal("0"), "deposits": Decimal("0"), "withdrawals": Decimal("0"),
                "calculated": Decimal("0"), "reported": Decimal("0"), "count": 0, "mismatch": 0,
            })
            for f in ("opening", "deposits", "withdrawals", "calculated"):
                t[f] += r[f]
            if r["reported"] is not None:
                t["reported"] += r["reported"]
            t["count"] += 1
            if not r["match"]:
                t["mismatch"] += 1

        return render_template(
            "opening_balance_reconciliation.html",
            results=results, totals=totals, cutoff=cutoff,
            mismatch_count=sum(1 for r in results if not r["match"]),
        )

    app.add_url_rule(
        "/data/opening-balances/reconcile",
        "opening_balance_reconcile",
        opening_balance_reconcile,
        methods=["GET"],
    )
    _registered = True
