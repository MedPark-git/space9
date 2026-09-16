import io
from collections import defaultdict
from datetime import date
from decimal import Decimal

from flask import Response, request
from flask_login import login_required
from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import func

import phase2_feature as p2
import app as pkg

core = pkg.core
db = pkg.db
app = pkg.app
REGISTERED = False


def _loan_balances_as_of(day):
    totals = defaultdict(Decimal)
    for account in core.LoanAccount.query.filter_by(status="active").all():
        tx = core.LoanTransaction.query.filter(
            core.LoanTransaction.loan_account_id == account.id,
            core.LoanTransaction.transaction_date <= day,
        ).order_by(
            core.LoanTransaction.transaction_date.desc(),
            core.LoanTransaction.source_row_number.asc(),
        ).first()
        if tx:
            totals[account.currency] += p2._dec(tx.loan_balance)
        elif account.loan_date and account.loan_date <= day:
            totals[account.currency] += p2._dec(account.current_balance)
    return totals


def _day_flows(day):
    totals = defaultdict(lambda: {"in": Decimal("0"), "out": Decimal("0")})
    for tx in core.BankTransaction.query.filter(core.BankTransaction.transaction_date == day).all():
        curr = tx.account.currency
        totals[curr]["in"] += p2._dec(tx.deposit_amount)
        totals[curr]["out"] += p2._dec(tx.withdrawal_amount)
    return {c: {k: str(v) for k, v in vals.items()} for c, vals in totals.items()}


def _snapshot(day):
    snap = p2._snapshot(day, pkg.MmtOpeningBalance)
    snap["loan_by_currency"] = {k: str(v) for k, v in _loan_balances_as_of(day).items()}
    snap["day_flows"] = _day_flows(day)
    return snap


def register():
    global REGISTERED
    if REGISTERED:
        return

    @login_required
    def dashboard_enhanced():
        day = p2._latest_transaction_date() or date.today()
        snap = _snapshot(day)
        latest = p2._latest_upload()
        unclassified = p2.TransactionAnnotation.query.filter(p2.TransactionAnnotation.category_id.is_(None)).count()
        body = """
        <div class="p2head"><div><h2>자금현황 대시보드</h2><p class="muted">기준일 {{ day }}</p></div><div class="actions"><a href="{{ url_for('cash_journal') }}?date={{ day }}">자금일보 보기</a><a class="secondary" href="{{ url_for('phase2_integrity') }}?date={{ day }}">데이터 정합성</a></div></div>
        <div class="kpis">
          <div class="kpi"><span>원화예금</span><b>{{ fmt(s.krw_total) }}원</b></div>
          <div class="kpi"><span>외화 원화환산</span><b>{{ fmt(s.fx_krw_total) }}원</b></div>
          <div class="kpi"><span>MMT</span><b>{{ fmt(s.mmt_total) }}원</b></div>
          <div class="kpi"><span>총 금융자금</span><b>{{ fmt(s.total_financial) }}원</b></div>
          <div class="kpi"><span>대출잔액(KRW)</span><b>{{ fmt(s.loan_by_currency.get('KRW','0')) }}원</b></div>
          <div class="kpi"><span>D+1 예정입금</span><b>{{ fmt(s.planned_in) }}원</b></div>
          <div class="kpi"><span>D+1 예정출금</span><b>{{ fmt(s.planned_out) }}원</b></div>
        </div>
        <section class="panel"><h3>당일 입출금</h3><div class="actions">{% for c,v in s.day_flows.items() %}<span class="badge ok">{{ c }} 입금 {{ fmt(v['in'],c) }} / 출금 {{ fmt(v['out'],c) }}</span>{% else %}<span class="muted">당일 거래 없음</span>{% endfor %}</div></section>
        <section class="panel"><h3>데이터 상태</h3>
          <table><tr><th>마지막 e-Branch 업로드</th><td>{{ latest.confirmed_at|kst if latest else '-' }}</td><th>거래자료 최종일</th><td>{{ day }}</td></tr>
          <tr><th>원화계좌</th><td>정상 {{ s.status.krw.ok }} / 확인필요 {{ s.status.krw.review }} / 자료부족 {{ s.status.krw.insufficient }}</td>
              <th>외화계좌</th><td>정상 {{ s.status.fx.ok }} / 확인필요 {{ s.status.fx.review }} / 자료부족 {{ s.status.fx.insufficient }}</td></tr>
          <tr><th>MMT</th><td>정상 {{ s.status.mmt.ok }} / 확인필요 {{ s.status.mmt.review }} / 자료부족 {{ s.status.mmt.insufficient }}</td>
              <th>미분류 거래</th><td>{{ unclassified }}건</td></tr></table>
          {% if s.ready %}<div class="okbox"><b>자금일보 생성 가능</b></div>{% else %}<div class="warnbox"><b>확인 필요</b> — 데이터 정합성 또는 환율을 확인해 주세요.</div>{% endif %}
        </section>
        """
        return p2._render("대시보드", body, s=snap, day=day, latest=latest, unclassified=unclassified)

    @login_required
    def cash_journal_enhanced():
        day = p2._d(request.args.get("date"), p2._latest_transaction_date() or date.today())
        versions = p2.CashJournalVersion.query.filter_by(journal_date=day).order_by(p2.CashJournalVersion.version.desc()).all()
        selected_version = None
        version_no = request.args.get("version")
        if version_no and str(version_no).isdigit():
            selected_version = p2.CashJournalVersion.query.filter_by(journal_date=day, version=int(version_no)).first()
        snap = selected_version.snapshot_json if selected_version else _snapshot(day)
        latest_upload = p2._latest_upload()
        latest_state = selected_version.status if selected_version else (versions[0].status if versions else "draft")
        body = """
        <div class="p2head"><div><h2>자금일보</h2><p class="muted">D-1 실적 · D-Day 잔액 · D+1 예정 · 상태 {{ {'draft':'작성중','reviewed':'검토완료','confirmed':'확정'}.get(latest_state,latest_state) }}{% if selected_version %} · Version {{ selected_version.version }} Snapshot{% endif %}</p></div><div class="actions noprint"><a href="{{ url_for('phase2_cash_journal_excel') }}?date={{ day }}{% if selected_version %}&version={{ selected_version.version }}{% endif %}">Excel 다운로드</a><a class="secondary" href="javascript:window.print()">인쇄</a></div></div>
        <form method="get" class="panel filters noprint"><label>기준일<input type="date" name="date" value="{{ day }}"></label><button>조회</button></form>
        <div class="kpis"><div class="kpi"><span>원화예금</span><b>{{ fmt(s.krw_total) }}원</b></div><div class="kpi"><span>외화 환산</span><b>{{ fmt(s.fx_krw_total) }}원</b></div><div class="kpi"><span>MMT</span><b>{{ fmt(s.mmt_total) }}원</b></div><div class="kpi"><span>총 금융자금</span><b>{{ fmt(s.total_financial) }}원</b></div><div class="kpi"><span>대출잔액(KRW)</span><b>{{ fmt(s.get('loan_by_currency',{}).get('KRW','0')) }}원</b></div><div class="kpi"><span>D+1 예상자금</span><b>{{ fmt(s.expected) }}원</b></div></div>
        <section class="panel"><h3>데이터 기준</h3><table><tr><th>마지막 e-Branch 업로드</th><td>{{ latest_upload.confirmed_at|kst if latest_upload else '-' }}</td><th>환율 기준일</th><td>{{ day if not s.missing_rates else '누락: ' + (s.missing_rates|join(', ')) }}</td></tr><tr><th>원화 검증</th><td>정상 {{ s.status.krw.ok }} / 확인필요 {{ s.status.krw.review }} / 자료부족 {{ s.status.krw.insufficient }}</td><th>외화 검증</th><td>정상 {{ s.status.fx.ok }} / 확인필요 {{ s.status.fx.review }} / 자료부족 {{ s.status.fx.insufficient }}</td></tr></table></section>
        {% if s.ready %}<div class="okbox">데이터 상태: <b>자금일보 확정 가능</b></div>{% else %}<div class="warnbox">데이터 상태: <b>확정 불가</b> — 잔액 불일치/자료부족/환율 누락을 확인하세요.{% if s.missing_rates %} 환율누락: {{ s.missing_rates|join(', ') }}{% endif %}</div>{% endif %}
        <h3 class="section-title">D-1 실적 — {{ s.d1 }}</h3><section class="panel"><table><thead><tr><th>통화</th><th class="num">외부입금</th><th class="num">외부출금</th><th class="num">내부대체 입금</th><th class="num">내부대체 출금</th></tr></thead><tbody>{% for c,v in s.actual_by_currency.items() %}<tr><td>{{ c }}</td><td class="num">{{ fmt(v.external_in,c) }}</td><td class="num">{{ fmt(v.external_out,c) }}</td><td class="num">{{ fmt(v.internal_in,c) }}</td><td class="num">{{ fmt(v.internal_out,c) }}</td></tr>{% else %}<tr><td colspan="5" class="empty">거래 없음</td></tr>{% endfor %}</tbody></table><div class="actions">{% for key,v in s.category_totals.items() %}{% set parts=key.split('|') %}<span class="badge ok">{{ parts[0] }} / {{ parts[1] }} {{ fmt(v,parts[1]) }}</span>{% endfor %}</div></section>
        <section class="panel scroll"><h3>주요 입출금 상세</h3><table><thead><tr><th>적요</th><th>상대방</th><th>통화</th><th class="num">입금</th><th class="num">출금</th><th>흐름</th><th>분류</th></tr></thead><tbody>{% for r in s.actual_details %}<tr><td>{{ r.description or '-' }}</td><td>{{ r.counterparty or '-' }}</td><td>{{ r.currency }}</td><td class="num">{{ fmt(r.deposit,r.currency) }}</td><td class="num">{{ fmt(r.withdrawal,r.currency) }}</td><td>{{ r.flow }}</td><td>{{ r.category or '-' }}</td></tr>{% else %}<tr><td colspan="7" class="empty">거래 없음</td></tr>{% endfor %}</tbody></table></section>
        <h3 class="section-title">D-Day 계좌별 자금잔액 — {{ s.date }}</h3><section class="panel scroll"><table><thead><tr><th>은행</th><th>계좌</th><th>통화</th><th class="num">원통화잔액</th><th class="num">적용환율</th><th class="num">원화환산</th><th>상태</th></tr></thead><tbody>{% for a in s.accounts %}<tr><td>{{ a.bank }}</td><td>{{ a.account }}</td><td>{{ a.currency }}</td><td class="num">{{ a.balance }}</td><td class="num">{{ a.rate or '-' }}</td><td class="num">{{ a.krw or '-' }}</td><td><span class="badge {{ a.status }}">{{ {'ok':'정상','review':'확인필요','insufficient':'자료부족'}[a.status] }}</span></td></tr>{% endfor %}</tbody></table></section>
        <h3 class="section-title">D+1 예정 — {{ s.dplus }}</h3><section class="panel"><div class="kpis"><div class="kpi"><span>예정입금(KRW환산)</span><b>{{ fmt(s.planned_in) }}</b></div><div class="kpi"><span>예정출금(KRW환산)</span><b>{{ fmt(s.planned_out) }}</b></div></div><table><thead><tr><th>입/출금</th><th>거래처</th><th>내용</th><th>통화</th><th class="num">금액</th><th>상태</th></tr></thead><tbody>{% for p in s.plans %}<tr><td>{{ '입금' if p.direction=='in' else '출금' }}</td><td>{{ p.counterparty }}</td><td>{{ p.description }}</td><td>{{ p.currency }}</td><td class="num">{{ p.amount }}</td><td>{{ p.status }}</td></tr>{% else %}<tr><td colspan="6" class="empty">예정내역 없음</td></tr>{% endfor %}</tbody></table></section>
        <section class="panel noprint"><h3>상태 / Version</h3>{% if not selected_version %}<form method="post" action="{{ url_for('phase2_cash_journal_action') }}" class="filters"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="date" value="{{ day }}"><label>수정사유/메모<input name="reason"></label><button name="action" value="review">검토완료 Snapshot</button><button name="action" value="confirm" {% if not s.ready %}disabled{% endif %}>확정 Snapshot</button></form>{% else %}<div class="okbox">저장된 Version {{ selected_version.version }} Snapshot 조회 중입니다. 현재 데이터는 변경되지 않습니다.</div>{% endif %}<p class="muted">기존 Version은 삭제하지 않습니다.</p><div class="actions"><a class="secondary" href="{{ url_for('cash_journal') }}?date={{ day }}">현재 데이터</a>{% for v in versions %}<a class="{{ 'secondary' if v.status!='confirmed' else '' }}" href="{{ url_for('cash_journal') }}?date={{ day }}&version={{ v.version }}">V{{ v.version }} {{ v.status }} {{ v.created_at|kst }}</a>{% endfor %}</div></section>
        """
        return p2._render("자금일보", body, day=day, s=snap, versions=versions, selected_version=selected_version, latest_state=latest_state, latest_upload=latest_upload)

    @core.roles_required("admin", "editor")
    def cash_journal_action_enhanced():
        day = p2._d(request.form.get("date"))
        action = request.form.get("action")
        if action not in ("review", "confirm") or not day:
            from flask import abort
            abort(400)
        snap = _snapshot(day)
        if action == "confirm" and not snap["ready"]:
            from flask import flash, redirect, url_for
            flash("잔액 불일치·자료부족·환율 누락이 있어 확정할 수 없습니다.", "error")
            return redirect(url_for("cash_journal", date=day.isoformat()))
        next_ver = (db.session.query(func.max(p2.CashJournalVersion.version)).filter_by(journal_date=day).scalar() or 0) + 1
        status = "confirmed" if action == "confirm" else "reviewed"
        from flask_login import current_user
        v = p2.CashJournalVersion(journal_date=day, version=next_ver, status=status, snapshot_json=snap, created_by=current_user.id)
        v.reviewed_by = current_user.id if action == "review" else None
        v.confirmed_by = current_user.id if action == "confirm" else None
        v.confirmed_at = core.utcnow() if action == "confirm" else None
        v.change_reason = (request.form.get("reason") or "").strip()
        db.session.add(v)
        core.audit("cash_journal_snapshot_created", user=current_user, target_type="cash_journal_version", target_id=v.id, detail=f"date={day} version={next_ver} status={status}")
        db.session.commit()
        from flask import flash, redirect, url_for
        flash(f"자금일보 Version {next_ver} ({status}) 저장 완료", "success")
        return redirect(url_for("cash_journal", date=day.isoformat()))

    @login_required
    def cash_journal_excel_enhanced():
        day = p2._d(request.args.get("date"), p2._latest_transaction_date() or date.today())
        version_no = request.args.get("version")
        version = None
        if version_no and str(version_no).isdigit():
            version = p2.CashJournalVersion.query.filter_by(journal_date=day, version=int(version_no)).first()
        snap = version.snapshot_json if version else _snapshot(day)
        wb = Workbook(); ws = wb.active; ws.title = "자금일보"
        ws.append(["MedPark 자금일보", day.isoformat()]); ws["A1"].font = Font(bold=True, size=16)
        ws.append(["원화예금", Decimal(snap["krw_total"])]); ws.append(["외화 원화환산", Decimal(snap["fx_krw_total"])]); ws.append(["MMT", Decimal(snap["mmt_total"])]); ws.append(["총 금융자금", Decimal(snap["total_financial"])]); ws.append(["대출잔액(KRW)", Decimal(snap.get("loan_by_currency", {}).get("KRW", "0"))]); ws.append(["D+1 예정입금", Decimal(snap["planned_in"])]); ws.append(["D+1 예정출금", Decimal(snap["planned_out"])]); ws.append(["D+1 예상자금", Decimal(snap["expected"])]); ws.append(["Version", version.version if version else "현재"]); ws.append(["상태", version.status if version else "작성중"])
        ws.append([]); ws.append(["은행", "계좌", "통화", "원통화잔액", "적용환율", "원화환산", "상태"])
        for a in snap["accounts"]: ws.append([a["bank"], a["account"], a["currency"], Decimal(a["balance"]), Decimal(a["rate"]) if a["rate"] else None, Decimal(a["krw"]) if a["krw"] else None, a["status"]])
        ws.append([]); ws.append(["D-1 적요", "상대방", "통화", "입금", "출금", "흐름", "분류"])
        for r in snap.get("actual_details", []): ws.append([r["description"], r["counterparty"], r["currency"], Decimal(r["deposit"]), Decimal(r["withdrawal"]), r["flow"], r["category"]])
        ws.append([]); ws.append(["D+1", "입/출금", "거래처", "내용", "통화", "금액", "상태"])
        for p in snap["plans"]: ws.append([snap["dplus"], "입금" if p["direction"] == "in" else "출금", p["counterparty"], p["description"], p["currency"], Decimal(p["amount"]), p["status"]])
        for col in "ABCDEFG": ws.column_dimensions[col].width = 18
        out = io.BytesIO(); wb.save(out); out.seek(0)
        return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=MedPark_CashJournal_{day.isoformat()}.xlsx"})

    app.view_functions["dashboard"] = dashboard_enhanced
    app.view_functions["cash_journal"] = cash_journal_enhanced
    app.view_functions["phase2_cash_journal_action"] = cash_journal_action_enhanced
    app.view_functions["phase2_cash_journal_excel"] = cash_journal_excel_enhanced
    REGISTERED = True
