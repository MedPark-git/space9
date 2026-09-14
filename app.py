import csv
import io
import json
import os
import shutil
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for, Response
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy import or_, text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from ebank_parser import EBranchFormatError, file_sha256, parse_file, select_final_balance

KST = ZoneInfo("Asia/Seoul")
REQUIRED_DB_ENV = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
SCHEMA_VERSION = 2
USER_DATA_ROOT = Path(os.getenv("USER_DATA_ROOT", "/app/user_data"))
PENDING_DIR = USER_DATA_ROOT / "pending"
IMPORT_DIR = USER_DATA_ROOT / "imports"
UPLOAD_NAMES = {"krw": "원화 거래내역", "fx": "외화 거래내역", "mmt": "MMT", "loan": "대출내역"}

def utcnow(): return datetime.now(timezone.utc)
def db_config_present(): return all(os.getenv(k) for k in REQUIRED_DB_ENV)
def database_uri():
    if not db_config_present(): return "postgresql+psycopg://not-ready:not-ready@127.0.0.1:1/not-ready"
    return (f"postgresql+psycopg://{quote_plus(os.environ['DB_USER'])}:" f"{quote_plus(os.environ['DB_PASSWORD'])}@{os.environ['DB_HOST']}:" f"{os.environ['DB_PORT']}/{os.environ['DB_NAME']}")

app = Flask(__name__)
app.config.update(SECRET_KEY=os.getenv("SECRET_KEY") or os.urandom(32), SQLALCHEMY_DATABASE_URI=database_uri(), SQLALCHEMY_TRACK_MODIFICATIONS=False, SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 300, "pool_size": 5, "max_overflow": 5}, PERMANENT_SESSION_LIFETIME=timedelta(minutes=60), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true", SESSION_COOKIE_SAMESITE="Lax", WTF_CSRF_TIME_LIMIT=3600, MAX_CONTENT_LENGTH=20 * 1024 * 1024)
db = SQLAlchemy(app); csrf = CSRFProtect(app); login_manager = LoginManager(app); login_manager.login_view = "login"; login_manager.login_message = "로그인이 필요합니다."

class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); login_id = db.Column(db.String(80), nullable=False, unique=True, index=True); name = db.Column(db.String(100), nullable=False); department = db.Column(db.String(100)); position = db.Column(db.String(100)); role = db.Column(db.String(20), nullable=False, default="viewer"); password_hash = db.Column(db.String(255), nullable=False); is_active_flag = db.Column(db.Boolean, nullable=False, default=True); must_change_password = db.Column(db.Boolean, nullable=False, default=True); last_login_at = db.Column(db.DateTime(timezone=True)); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    @property
    def is_active(self): return self.is_active_flag
    def get_id(self): return str(self.id)

class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); user_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("users.id"), nullable=True, index=True); login_id = db.Column(db.String(80), index=True); action = db.Column(db.String(80), nullable=False, index=True); target_type = db.Column(db.String(80)); target_id = db.Column(db.String(120)); detail = db.Column(db.String(500)); ip_address = db.Column(db.String(64)); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

class CashJournal(db.Model):
    __tablename__ = "cash_journals"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); journal_date = db.Column(db.Date, nullable=False, index=True); status = db.Column(db.String(20), nullable=False, default="draft"); note = db.Column(db.Text); created_by = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("users.id"), nullable=False); updated_by = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("users.id"), nullable=False); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

class ImportBatch(db.Model):
    __tablename__ = "import_batches"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); batch_code = db.Column(db.String(32), nullable=False, unique=True, index=True); file_type = db.Column(db.String(20), nullable=False, index=True); original_filename = db.Column(db.String(255), nullable=False); stored_path = db.Column(db.String(500)); file_sha256 = db.Column(db.String(64), nullable=False, index=True); structure_hash = db.Column(db.String(64), nullable=False); query_start_date = db.Column(db.Date); query_end_date = db.Column(db.Date); total_rows = db.Column(db.Integer, nullable=False, default=0); new_rows = db.Column(db.Integer, nullable=False, default=0); duplicate_rows = db.Column(db.Integer, nullable=False, default=0); review_rows = db.Column(db.Integer, nullable=False, default=0); error_rows = db.Column(db.Integer, nullable=False, default=0); status = db.Column(db.String(20), nullable=False, default="confirmed"); created_by = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("users.id"), nullable=False); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); confirmed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); creator = db.relationship("User", foreign_keys=[created_by])

class ImportRawRow(db.Model):
    __tablename__ = "import_raw_rows"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); import_batch_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("import_batches.id"), nullable=False, index=True); source_row_number = db.Column(db.Integer, nullable=False); row_status = db.Column(db.String(20), nullable=False); message = db.Column(db.String(500)); raw_data = db.Column(db.JSON, nullable=False); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); batch = db.relationship("ImportBatch")

class BankAccount(db.Model):
    __tablename__ = "bank_accounts"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); financial_institution = db.Column(db.String(80), nullable=False); account_number = db.Column(db.String(100), nullable=False); account_name = db.Column(db.String(200)); currency = db.Column(db.String(10), nullable=False, default="KRW"); account_type = db.Column(db.String(30), nullable=False, default="deposit"); is_active = db.Column(db.Boolean, nullable=False, default=True); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (db.UniqueConstraint("financial_institution", "account_number", "currency", name="uq_bank_account_identity"),)

class BankTransaction(db.Model):
    __tablename__ = "bank_transactions"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); account_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("bank_accounts.id"), nullable=False, index=True); transaction_date = db.Column(db.Date, nullable=False, index=True); transaction_time = db.Column(db.Time); deposit_amount = db.Column(db.Numeric(28, 6), nullable=False, default=0); withdrawal_amount = db.Column(db.Numeric(28, 6), nullable=False, default=0); balance = db.Column(db.Numeric(28, 6), nullable=False); description = db.Column(db.String(500)); detail1 = db.Column(db.String(500)); detail2 = db.Column(db.String(500)); counterparty = db.Column(db.String(300)); counter_account = db.Column(db.String(200)); branch = db.Column(db.String(200)); account_subject = db.Column(db.String(200)); cms_number = db.Column(db.String(200)); financial_institution = db.Column(db.String(80), nullable=False); source_row_number = db.Column(db.Integer, nullable=False); import_batch_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("import_batches.id"), nullable=False, index=True); transaction_hash = db.Column(db.String(64), nullable=False, unique=True, index=True); created_by = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("users.id"), nullable=False); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); account = db.relationship("BankAccount"); batch = db.relationship("ImportBatch")

class OpeningBalance(db.Model):
    __tablename__ = "opening_balances"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); base_date = db.Column(db.Date, nullable=False, index=True); bank_account_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("bank_accounts.id"), nullable=False, index=True); currency = db.Column(db.String(10), nullable=False); source_date = db.Column(db.Date); source_balance = db.Column(db.Numeric(28, 6)); opening_balance = db.Column(db.Numeric(28, 6)); status = db.Column(db.String(30), nullable=False, default="needs_review"); calculation_method = db.Column(db.String(500)); source_batch_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("import_batches.id")); source_transaction_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("bank_transactions.id")); created_by = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("users.id"), nullable=False); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow); account = db.relationship("BankAccount"); source_batch = db.relationship("ImportBatch")
    __table_args__ = (db.UniqueConstraint("base_date", "bank_account_id", name="uq_opening_balance_account_date"),)

class MmtTransaction(db.Model):
    __tablename__ = "mmt_transactions"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); financial_institution = db.Column(db.String(80), nullable=False); account_number = db.Column(db.String(100), nullable=False, index=True); account_name = db.Column(db.String(200)); currency = db.Column(db.String(10), nullable=False, default="KRW"); transaction_date = db.Column(db.Date, nullable=False, index=True); transaction_time = db.Column(db.Time); deposit_amount = db.Column(db.Numeric(28, 6), nullable=False, default=0); withdrawal_amount = db.Column(db.Numeric(28, 6), nullable=False, default=0); balance = db.Column(db.Numeric(28, 6), nullable=False); description = db.Column(db.String(500)); detail1 = db.Column(db.String(500)); detail2 = db.Column(db.String(500)); branch = db.Column(db.String(200)); source_row_number = db.Column(db.Integer, nullable=False); import_batch_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("import_batches.id"), nullable=False, index=True); transaction_hash = db.Column(db.String(64), nullable=False, unique=True, index=True); created_by = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("users.id"), nullable=False); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); batch = db.relationship("ImportBatch")

class LoanAccount(db.Model):
    __tablename__ = "loan_accounts"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); financial_institution = db.Column(db.String(80), nullable=False); account_number = db.Column(db.String(100), nullable=False); currency = db.Column(db.String(10), nullable=False, default="KRW"); loan_product = db.Column(db.String(250)); agreed_amount = db.Column(db.Numeric(28, 6)); execution_amount = db.Column(db.Numeric(28, 6)); current_balance = db.Column(db.Numeric(28, 6)); interest_rate = db.Column(db.Numeric(12, 6)); loan_date = db.Column(db.Date); maturity_date = db.Column(db.Date); last_transaction_date = db.Column(db.Date); status = db.Column(db.String(30), nullable=False, default="active"); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (db.UniqueConstraint("financial_institution", "account_number", "currency", name="uq_loan_account_identity"),)

class LoanTransaction(db.Model):
    __tablename__ = "loan_transactions"
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4); loan_account_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("loan_accounts.id"), nullable=False, index=True); transaction_date = db.Column(db.Date, nullable=False, index=True); transaction_type = db.Column(db.String(100)); transaction_amount = db.Column(db.Numeric(28, 6), nullable=False, default=0); interest_amount = db.Column(db.Numeric(28, 6), nullable=False, default=0); principal_amount = db.Column(db.Numeric(28, 6), nullable=False, default=0); loan_balance = db.Column(db.Numeric(28, 6), nullable=False, default=0); interest_rate = db.Column(db.Numeric(12, 6)); interest_calc_start = db.Column(db.Date); interest_calc_end = db.Column(db.Date); interest_calc_days = db.Column(db.Integer); applied_fx_rate = db.Column(db.Numeric(28, 6)); loan_krw_amount = db.Column(db.Numeric(28, 6)); detail1 = db.Column(db.String(500)); detail2 = db.Column(db.String(500)); detail3 = db.Column(db.String(500)); note1 = db.Column(db.String(500)); note2 = db.Column(db.String(500)); source_row_number = db.Column(db.Integer, nullable=False); import_batch_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("import_batches.id"), nullable=False, index=True); transaction_hash = db.Column(db.String(64), nullable=False, unique=True, index=True); created_by = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("users.id"), nullable=False); created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow); loan_account = db.relationship("LoanAccount"); batch = db.relationship("ImportBatch")

class SchemaVersion(db.Model):
    __tablename__ = "schema_versions"
    version = db.Column(db.Integer, primary_key=True); applied_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

@login_manager.user_loader
def load_user(user_id):
    try: return db.session.get(User, uuid.UUID(user_id))
    except Exception: return None

def kst(value):
    if not value: return "-"
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
def amount(value, currency=None):
    if value is None: return "-"
    value = Decimal(value); decimals = 0 if currency == "KRW" else min(6, max(2, -value.as_tuple().exponent)); return f"{value:,.{decimals}f}"
app.jinja_env.filters["kst"] = kst; app.jinja_env.filters["amount"] = amount

def audit(action, user=None, login_id=None, target_type=None, target_id=None, detail=None):
    db.session.add(AuditLog(user_id=getattr(user, "id", None), login_id=login_id or getattr(user, "login_id", None), action=action, target_type=target_type, target_id=str(target_id) if target_id else None, detail=(detail or "")[:500], ip_address=(request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip())[:64]))
def commit_or_rollback():
    try: db.session.commit(); return True
    except SQLAlchemyError: db.session.rollback(); app.logger.exception("Database transaction failed"); return False

def ensure_schema_and_bootstrap():
    if not db_config_present(): raise RuntimeError("PostgreSQL 환경변수가 준비되지 않았습니다.")
    with db.engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(874221934)")); db.metadata.create_all(bind=conn); current = conn.execute(text("SELECT COALESCE(MAX(version), 0) FROM schema_versions")).scalar_one()
        if current < SCHEMA_VERSION: conn.execute(text("INSERT INTO schema_versions(version, applied_at) VALUES (:v, NOW()) ON CONFLICT DO NOTHING"), {"v": SCHEMA_VERSION})
    if User.query.count() == 0:
        admin_id = os.getenv("BOOTSTRAP_ADMIN_ID"); admin_pw = os.getenv("BOOTSTRAP_ADMIN_PASSWORD"); admin_name = os.getenv("BOOTSTRAP_ADMIN_NAME", "시스템관리자")
        if not admin_id or not admin_pw: raise RuntimeError("초기 관리자 환경변수가 준비되지 않았습니다.")
        db.session.add(User(login_id=admin_id.strip().lower(), name=admin_name, role="admin", password_hash=generate_password_hash(admin_pw), is_active_flag=True, must_change_password=True)); db.session.commit()
    PENDING_DIR.mkdir(parents=True, exist_ok=True); IMPORT_DIR.mkdir(parents=True, exist_ok=True)

@app.before_request
def prepare_request():
    session.permanent = True
    if request.endpoint == "static": return None
    try: ensure_schema_and_bootstrap()
    except Exception:
        db.session.rollback()
        if request.path == "/health": return None
        if request.path.startswith("/api/"): return jsonify({"error": "application_not_ready"}), 503
        if request.path != "/not-ready": return redirect(url_for("not_ready"))
    if current_user.is_authenticated and current_user.must_change_password and request.endpoint not in {"force_password_change", "logout", "not_ready"}: return redirect(url_for("force_password_change"))

@app.teardown_request
def teardown_request(exc):
    if exc: db.session.rollback()
    db.session.remove()
@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"): return jsonify({"error": "unauthorized"}), 401
    return redirect(url_for("login", next=request.full_path))
def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles: abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return deco

def recent_failed_attempts(login_id): return AuditLog.query.filter(AuditLog.action == "login_failed", AuditLog.login_id == login_id, AuditLog.created_at >= utcnow() - timedelta(minutes=10)).count()
def parse_iso_date(value):
    if not value: return None
    try: return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError: return None
def parse_iso_time(value):
    if not value: return None
    try: return datetime.strptime(value, "%H:%M:%S").time()
    except ValueError: return None
def dec(value):
    if value in (None, ""): return None
    return Decimal(str(value))
def existing_hashes(file_type, hashes):
    model = BankTransaction if file_type in {"krw", "fx"} else MmtTransaction if file_type == "mmt" else LoanTransaction; found = set(); hashes = list(hashes)
    for i in range(0, len(hashes), 1000): found.update(x[0] for x in db.session.query(model.transaction_hash).filter(model.transaction_hash.in_(hashes[i:i+1000])).all())
    return found
def next_batch_code():
    prefix = datetime.now(KST).strftime("IMP-%Y%m%d-"); codes = [x[0] for x in db.session.query(ImportBatch.batch_code).filter(ImportBatch.batch_code.like(prefix + "%")).all()]; seq = max([int(c.rsplit("-", 1)[-1]) for c in codes if c.rsplit("-", 1)[-1].isdigit()] or [0]) + 1; return f"{prefix}{seq:03d}"
def get_or_create_bank_account(cache, row):
    key = (row["bank"], row["account_number"], row["currency"])
    if key not in cache:
        account = BankAccount.query.filter_by(financial_institution=key[0], account_number=key[1], currency=key[2]).first()
        if not account:
            account = BankAccount(financial_institution=key[0], account_number=key[1], currency=key[2], account_name=row.get("account_name"), account_type="deposit"); db.session.add(account); db.session.flush()
        elif row.get("account_name") and not account.account_name: account.account_name = row["account_name"]
        cache[key] = account
    return cache[key]
def get_or_create_loan_account(cache, row):
    key = (row["bank"], row["account_number"], row["currency"])
    if key not in cache:
        account = LoanAccount.query.filter_by(financial_institution=key[0], account_number=key[1], currency=key[2]).first()
        if not account:
            account = LoanAccount(financial_institution=key[0], account_number=key[1], currency=key[2], loan_product=row.get("loan_name")); db.session.add(account); db.session.flush()
        cache[key] = account
    account = cache[key]; tx_date = parse_iso_date(row["transaction_date"])
    if account.last_transaction_date is None or tx_date > account.last_transaction_date:
        account.current_balance = dec(row.get("loan_balance")); account.interest_rate = dec(row.get("interest_rate")); account.last_transaction_date = tx_date
        if row.get("loan_name"): account.loan_product = row["loan_name"]
    return account
def preview_path(token): return PENDING_DIR / f"{token}.json"
def load_preview(token):
    path = preview_path(token)
    if not path.exists(): abort(404)
    with path.open("r", encoding="utf-8") as f: data = json.load(f)
    if data.get("created_by") != str(current_user.id) and current_user.role != "admin": abort(403)
    return data

@app.route("/not-ready")
def not_ready(): return render_template("not_ready.html"), 503
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated: return redirect(url_for("dashboard"))
    if request.method == "POST":
        login_id = (request.form.get("login_id") or "").strip().lower(); password = request.form.get("password") or ""
        if recent_failed_attempts(login_id) >= 5: flash("로그인 실패 횟수가 초과되었습니다. 10분 후 다시 시도해 주세요.", "error"); return render_template("login.html"), 429
        user = User.query.filter(db.func.lower(User.login_id) == login_id).first()
        if not user or not user.is_active_flag or not check_password_hash(user.password_hash, password): audit("login_failed", login_id=login_id, detail="Invalid credentials or inactive account"); commit_or_rollback(); flash("아이디 또는 비밀번호를 확인해 주세요.", "error"); return render_template("login.html"), 401
        login_user(user); user.last_login_at = utcnow(); audit("login_success", user=user); commit_or_rollback(); return redirect(url_for("force_password_change" if user.must_change_password else "dashboard"))
    return render_template("login.html")
@app.route("/logout", methods=["POST"])
@login_required
def logout():
    audit("logout", user=current_user); commit_or_rollback(); logout_user(); session.clear(); response = redirect(url_for("login")); response.delete_cookie(app.config.get("SESSION_COOKIE_NAME", "session")); return response
@app.route("/password/initial", methods=["GET", "POST"])
@login_required
def force_password_change():
    if request.method == "POST":
        current = request.form.get("current_password") or ""; new1 = request.form.get("new_password") or ""; new2 = request.form.get("confirm_password") or ""
        if not check_password_hash(current_user.password_hash, current): flash("현재 비밀번호가 일치하지 않습니다.", "error")
        elif len(new1) < 10 or not any(c.isalpha() for c in new1) or not any(c.isdigit() for c in new1) or not any(not c.isalnum() for c in new1): flash("새 비밀번호는 10자 이상이며 영문·숫자·특수문자를 포함해야 합니다.", "error")
        elif new1 != new2: flash("새 비밀번호 확인이 일치하지 않습니다.", "error")
        elif check_password_hash(current_user.password_hash, new1): flash("기존 비밀번호와 다른 비밀번호를 사용해 주세요.", "error")
        else:
            current_user.password_hash = generate_password_hash(new1); current_user.must_change_password = False; audit("password_changed", user=current_user)
            if commit_or_rollback(): flash("비밀번호가 변경되었습니다.", "success"); return redirect(url_for("dashboard"))
            flash("비밀번호 변경에 실패했습니다.", "error")
    return render_template("password_change.html", initial=True)
@app.route("/password/change", methods=["GET", "POST"])
@login_required
def password_change(): return force_password_change()
@app.route("/")
@login_required
def dashboard():
    stats = {"bank_transactions": BankTransaction.query.count(), "mmt_transactions": MmtTransaction.query.count(), "loan_transactions": LoanTransaction.query.count(), "import_batches": ImportBatch.query.count()}; return render_template("dashboard.html", stats=stats)
@app.route("/cash-journal")
@login_required
def cash_journal(): return render_template("cash_journal.html", rows=CashJournal.query.order_by(CashJournal.journal_date.desc()).limit(100).all())

@app.route("/data/upload", methods=["GET", "POST"])
@login_required
def data_upload():
    if request.method == "POST":
        if current_user.role not in {"admin", "editor"}: abort(403)
        upload_type = request.form.get("upload_type"); uploaded = request.files.get("file")
        if upload_type not in UPLOAD_NAMES or not uploaded or not uploaded.filename: flash("업로드 유형과 파일을 확인해 주세요.", "error"); return redirect(url_for("data_upload"))
        if Path(uploaded.filename).suffix.lower() != ".xls": flash("현재 기준 Template과 동일한 기업은행 .xls 파일만 등록할 수 있습니다.", "error"); return redirect(url_for("data_upload"))
        token = uuid.uuid4().hex; xls_path = PENDING_DIR / f"{token}.xls"; uploaded.save(xls_path)
        try:
            parsed = parse_file(xls_path, upload_type); hashes = [r["transaction_hash"] for r in parsed["rows"]]; duplicated = existing_hashes(upload_type, hashes); new_count = sum(1 for h in hashes if h not in duplicated)
            preview = {"token": token, "created_by": str(current_user.id), "original_filename": uploaded.filename, "safe_filename": secure_filename(uploaded.filename), "file_type": parsed["file_type"], "file_sha256": file_sha256(xls_path), "structure_hash": parsed["structure_hash"], "query_start_date": parsed["query_start_date"], "query_end_date": parsed["query_end_date"], "rows": parsed["rows"], "reviews": parsed["reviews"], "errors": parsed["errors"], "duplicate_hashes": list(duplicated), "counts": {"total": len(parsed["rows"]) + len(parsed["reviews"]) + len(parsed["errors"]), "new": new_count, "duplicate": len(parsed["rows"]) - new_count, "review": len(parsed["reviews"]), "error": len(parsed["errors"])}}
            with preview_path(token).open("w", encoding="utf-8") as f: json.dump(preview, f, ensure_ascii=False)
            audit("import_preview_created", user=current_user, target_type="import", target_id=token, detail=f"{upload_type} {uploaded.filename}"); commit_or_rollback(); return redirect(url_for("import_preview", token=token))
        except EBranchFormatError as exc: xls_path.unlink(missing_ok=True); flash(str(exc), "error")
        except Exception: app.logger.exception("Import preview failed"); xls_path.unlink(missing_ok=True); flash("파일 분석 중 오류가 발생했습니다. 데이터는 등록되지 않았습니다.", "error")
        return redirect(url_for("data_upload"))
    batches = ImportBatch.query.order_by(ImportBatch.created_at.desc()).limit(30).all(); return render_template("data_upload.html", batches=batches, upload_names=UPLOAD_NAMES)
@app.route("/data/upload/preview/<token>")
@login_required
def import_preview(token):
    data = load_preview(token); dup = set(data.get("duplicate_hashes", [])); preview_rows = []
    for r in data["rows"][:100]: item = dict(r); item["duplicate"] = r["transaction_hash"] in dup; preview_rows.append(item)
    return render_template("import_preview.html", data=data, preview_rows=preview_rows, upload_names=UPLOAD_NAMES)
@app.route("/data/upload/confirm/<token>", methods=["POST"])
@roles_required("admin", "editor")
def import_confirm(token):
    data = load_preview(token); xls_path = PENDING_DIR / f"{token}.xls"
    if not xls_path.exists() or file_sha256(xls_path) != data["file_sha256"]: flash("업로드 원본 파일 검증에 실패했습니다. 다시 업로드해 주세요.", "error"); return redirect(url_for("data_upload"))
    try:
        parsed = parse_file(xls_path, data["file_type"]); hashes = [r["transaction_hash"] for r in parsed["rows"]]; duplicated = existing_hashes(data["file_type"], hashes); batch_code = next_batch_code(); stored_file = IMPORT_DIR / f"{batch_code}.xls"; shutil.copy2(xls_path, stored_file)
        batch = ImportBatch(batch_code=batch_code, file_type=data["file_type"], original_filename=data["original_filename"], stored_path=str(stored_file), file_sha256=data["file_sha256"], structure_hash=parsed["structure_hash"], query_start_date=parse_iso_date(parsed["query_start_date"]), query_end_date=parse_iso_date(parsed["query_end_date"]), total_rows=len(parsed["rows"]) + len(parsed["reviews"]) + len(parsed["errors"]), new_rows=sum(1 for r in parsed["rows"] if r["transaction_hash"] not in duplicated), duplicate_rows=sum(1 for r in parsed["rows"] if r["transaction_hash"] in duplicated), review_rows=len(parsed["reviews"]), error_rows=len(parsed["errors"]), status="confirmed", created_by=current_user.id, confirmed_at=utcnow()); db.session.add(batch); db.session.flush(); bank_cache = {}; loan_cache = {}
        for row in parsed["rows"]:
            is_dup = row["transaction_hash"] in duplicated; db.session.add(ImportRawRow(import_batch_id=batch.id, source_row_number=row["source_row_number"], row_status="duplicate" if is_dup else "new", raw_data=row["raw"]))
            if is_dup: continue
            if data["file_type"] in {"krw", "fx"}:
                account = get_or_create_bank_account(bank_cache, row); db.session.add(BankTransaction(account_id=account.id, transaction_date=parse_iso_date(row["transaction_date"]), transaction_time=parse_iso_time(row.get("transaction_time")), deposit_amount=dec(row["deposit"]), withdrawal_amount=dec(row["withdrawal"]), balance=dec(row["balance"]), description=row.get("description"), detail1=row.get("detail1"), detail2=row.get("detail2"), counterparty=row.get("counterparty"), counter_account=row.get("counter_account"), branch=row.get("branch"), account_subject=row.get("account_subject"), cms_number=row.get("cms_number"), financial_institution=row["bank"], source_row_number=row["source_row_number"], import_batch_id=batch.id, transaction_hash=row["transaction_hash"], created_by=current_user.id))
            elif data["file_type"] == "mmt":
                db.session.add(MmtTransaction(financial_institution=row["bank"], account_number=row["account_number"], account_name=row.get("account_name"), currency="KRW", transaction_date=parse_iso_date(row["transaction_date"]), transaction_time=parse_iso_time(row.get("transaction_time")), deposit_amount=dec(row["deposit"]), withdrawal_amount=dec(row["withdrawal"]), balance=dec(row["balance"]), description=row.get("description"), detail1=row.get("detail1"), detail2=row.get("detail2"), branch=row.get("branch"), source_row_number=row["source_row_number"], import_batch_id=batch.id, transaction_hash=row["transaction_hash"], created_by=current_user.id))
            else:
                account = get_or_create_loan_account(loan_cache, row); db.session.add(LoanTransaction(loan_account_id=account.id, transaction_date=parse_iso_date(row["transaction_date"]), transaction_type=row.get("transaction_type"), transaction_amount=dec(row.get("transaction_amount")) or Decimal("0"), interest_amount=dec(row.get("interest_amount")) or Decimal("0"), principal_amount=dec(row.get("principal_amount")) or Decimal("0"), loan_balance=dec(row.get("loan_balance")) or Decimal("0"), interest_rate=dec(row.get("interest_rate")), interest_calc_start=parse_iso_date(row.get("interest_start")), interest_calc_end=parse_iso_date(row.get("interest_end")), interest_calc_days=int(row["interest_days"]) if str(row.get("interest_days") or "").isdigit() else None, applied_fx_rate=dec(row.get("applied_fx_rate")), loan_krw_amount=dec(row.get("loan_krw_amount")), detail1=row.get("detail1"), detail2=row.get("detail2"), detail3=row.get("detail3"), note1=row.get("note1"), note2=row.get("note2"), source_row_number=row["source_row_number"], import_batch_id=batch.id, transaction_hash=row["transaction_hash"], created_by=current_user.id))
        for row in parsed["reviews"]: db.session.add(ImportRawRow(import_batch_id=batch.id, source_row_number=row["source_row_number"], row_status="review", message=row["message"], raw_data=row["raw"]))
        for row in parsed["errors"]: db.session.add(ImportRawRow(import_batch_id=batch.id, source_row_number=row["source_row_number"], row_status="error", message=row["message"], raw_data=row["raw"]))
        audit("import_confirmed", user=current_user, target_type="import_batch", target_id=batch.id, detail=f"{batch_code} {data['file_type']} new={batch.new_rows} duplicate={batch.duplicate_rows} review={batch.review_rows} error={batch.error_rows}"); db.session.commit(); xls_path.unlink(missing_ok=True); preview_path(token).unlink(missing_ok=True); flash(f"{batch_code} 등록이 완료되었습니다. 신규 {batch.new_rows:,}건 / 중복 {batch.duplicate_rows:,}건 / 확인필요 {batch.review_rows:,}건", "success"); return redirect(url_for("import_batch_detail", batch_id=batch.id))
    except Exception:
        db.session.rollback()
        if 'stored_file' in locals(): Path(stored_file).unlink(missing_ok=True)
        app.logger.exception("Import confirmation failed"); flash("등록 확정 중 오류가 발생했습니다. DB에는 반영되지 않았습니다.", "error"); return redirect(url_for("import_preview", token=token))
@app.route("/data/import-batches/<uuid:batch_id>")
@login_required
def import_batch_detail(batch_id):
    batch = db.get_or_404(ImportBatch, batch_id); raw_rows = ImportRawRow.query.filter_by(import_batch_id=batch.id).order_by(ImportRawRow.source_row_number).limit(200).all(); return render_template("import_batch.html", batch=batch, raw_rows=raw_rows, upload_names=UPLOAD_NAMES)
@app.route("/data/transactions")
@login_required
def financial_transactions():
    q = BankTransaction.query.join(BankAccount); start = parse_iso_date(request.args.get("start")); end = parse_iso_date(request.args.get("end")); bank = (request.args.get("bank") or "").strip(); account = (request.args.get("account") or "").strip(); data_type = request.args.get("data_type") or ""; currency = (request.args.get("currency") or "").strip().upper(); direction = request.args.get("direction") or ""; keyword = (request.args.get("q") or "").strip()
    if start: q = q.filter(BankTransaction.transaction_date >= start)
    if end: q = q.filter(BankTransaction.transaction_date <= end)
    if bank: q = q.filter(BankAccount.financial_institution == bank)
    if account: q = q.filter(BankAccount.account_number == account)
    if data_type == "krw": q = q.filter(BankAccount.currency == "KRW")
    elif data_type == "fx": q = q.filter(BankAccount.currency != "KRW")
    if currency: q = q.filter(BankAccount.currency == currency)
    if direction == "deposit": q = q.filter(BankTransaction.deposit_amount > 0)
    elif direction == "withdrawal": q = q.filter(BankTransaction.withdrawal_amount > 0)
    if keyword:
        like = f"%{keyword}%"; q = q.filter(or_(BankTransaction.description.ilike(like), BankTransaction.counterparty.ilike(like), BankTransaction.detail1.ilike(like), BankTransaction.detail2.ilike(like)))
    rows = q.order_by(BankTransaction.transaction_date.desc(), BankTransaction.transaction_time.desc().nullslast(), BankTransaction.source_row_number.asc()).limit(500).all(); banks = [x[0] for x in db.session.query(BankAccount.financial_institution).distinct().order_by(BankAccount.financial_institution).all()]; accounts = BankAccount.query.order_by(BankAccount.financial_institution, BankAccount.account_number).all(); currencies = [x[0] for x in db.session.query(BankAccount.currency).distinct().order_by(BankAccount.currency).all()]; balances = {}
    for a in accounts:
        latest = BankTransaction.query.filter_by(account_id=a.id).order_by(BankTransaction.transaction_date.desc(), BankTransaction.transaction_time.desc().nullslast(), BankTransaction.source_row_number.asc()).first()
        if latest: balances[a.currency] = balances.get(a.currency, Decimal("0")) + latest.balance
    return render_template("transactions.html", rows=rows, banks=banks, accounts=accounts, currencies=currencies, balances=balances)
@app.route("/data/transactions.csv")
@login_required
def financial_transactions_csv():
    rows = BankTransaction.query.join(BankAccount).order_by(BankTransaction.transaction_date, BankTransaction.transaction_time, BankTransaction.source_row_number).all(); out = io.StringIO(); w = csv.writer(out); w.writerow(["일자","시간","은행","계좌","통화","적요","입금","출금","잔액","Import Batch"])
    for r in rows: w.writerow([r.transaction_date.isoformat(), r.transaction_time.isoformat() if r.transaction_time else "", r.account.financial_institution, r.account.account_number, r.account.currency, r.description or "", r.deposit_amount, r.withdrawal_amount, r.balance, r.batch.batch_code])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=bank_transactions.csv"})
@app.route("/data/opening-balances", methods=["GET"])
@login_required
def opening_balances():
    base_date = date(2026, 1, 1); rows = OpeningBalance.query.filter_by(base_date=base_date).join(BankAccount).order_by(BankAccount.financial_institution, BankAccount.account_number, BankAccount.currency).all(); return render_template("opening_balances.html", rows=rows, base_date=base_date)
@app.route("/data/opening-balances/generate", methods=["POST"])
@roles_required("admin", "editor")
def generate_opening_balances():
    base_date = date(2026, 1, 1); source_date = date(2025, 12, 31); accounts = BankAccount.query.order_by(BankAccount.financial_institution, BankAccount.account_number, BankAccount.currency).all(); confirmed = review = 0
    for account in accounts:
        txs = BankTransaction.query.filter_by(account_id=account.id, transaction_date=source_date).order_by(BankTransaction.source_row_number).all(); helper_rows = [{"id":str(t.id), "transaction_time":t.transaction_time.isoformat() if t.transaction_time else None, "balance":str(t.balance), "deposit":str(t.deposit_amount), "withdrawal":str(t.withdrawal_amount)} for t in txs]; final_data, status_label, method = select_final_balance(helper_rows); ob = OpeningBalance.query.filter_by(base_date=base_date, bank_account_id=account.id).first()
        if not ob: ob = OpeningBalance(base_date=base_date, bank_account_id=account.id, currency=account.currency, created_by=current_user.id); db.session.add(ob)
        ob.source_date = source_date; ob.calculation_method = method
        if final_data:
            tx = next(t for t in txs if str(t.id) == final_data["id"]); ob.source_balance = tx.balance; ob.opening_balance = tx.balance; ob.status = "confirmed"; ob.source_batch_id = tx.import_batch_id; ob.source_transaction_id = tx.id; confirmed += 1
        else: ob.source_balance = None; ob.opening_balance = None; ob.status = "needs_review"; ob.source_batch_id = None; ob.source_transaction_id = None; review += 1
    audit("opening_balances_generated", user=current_user, target_type="opening_balance", target_id=base_date.isoformat(), detail=f"confirmed={confirmed}, review={review}")
    if commit_or_rollback(): flash(f"2026.01.01 기초잔액 생성 완료: 확인 {confirmed}계좌 / 확인필요 {review}계좌", "success")
    else: flash("기초잔액 생성에 실패했습니다.", "error")
    return redirect(url_for("opening_balances"))
@app.route("/data/mmt")
@login_required
def mmt():
    q = MmtTransaction.query; start = parse_iso_date(request.args.get("start")); end = parse_iso_date(request.args.get("end")); keyword = (request.args.get("q") or "").strip()
    if start: q = q.filter(MmtTransaction.transaction_date >= start)
    if end: q = q.filter(MmtTransaction.transaction_date <= end)
    if keyword:
        like = f"%{keyword}%"; q = q.filter(or_(MmtTransaction.description.ilike(like), MmtTransaction.account_number.ilike(like), MmtTransaction.account_name.ilike(like)))
    rows = q.order_by(MmtTransaction.transaction_date.desc(), MmtTransaction.transaction_time.desc().nullslast(), MmtTransaction.source_row_number.asc()).limit(500).all(); latest = MmtTransaction.query.order_by(MmtTransaction.transaction_date.desc(), MmtTransaction.transaction_time.desc().nullslast(), MmtTransaction.source_row_number.asc()).first(); return render_template("mmt.html", rows=rows, latest=latest)
@app.route("/data/loans")
@login_required
def loans():
    accounts = LoanAccount.query.order_by(LoanAccount.financial_institution, LoanAccount.account_number).all(); rows = LoanTransaction.query.order_by(LoanTransaction.transaction_date.desc(), LoanTransaction.source_row_number.asc()).limit(500).all(); return render_template("loans.html", accounts=accounts, rows=rows)

@app.route("/users", methods=["GET", "POST"])
@roles_required("admin")
def users():
    if request.method == "POST":
        login_id = (request.form.get("login_id") or "").strip().lower()
        if not login_id or User.query.filter(db.func.lower(User.login_id) == login_id).first(): flash("로그인 ID가 비어 있거나 이미 존재합니다.", "error")
        else:
            temp_password = uuid.uuid4().hex[:8] + "!9Aa"; user = User(login_id=login_id, name=(request.form.get("name") or login_id).strip(), department=(request.form.get("department") or "").strip(), position=(request.form.get("position") or "").strip(), role=request.form.get("role") if request.form.get("role") in {"admin", "editor", "viewer"} else "viewer", password_hash=generate_password_hash(temp_password), must_change_password=True); db.session.add(user); audit("user_created", user=current_user, target_type="user", target_id=user.id, detail=f"login_id={login_id}")
            if commit_or_rollback(): flash(f"사용자가 생성되었습니다. 임시 비밀번호: {temp_password}", "success")
            else: flash("사용자 생성에 실패했습니다.", "error")
    return render_template("users.html", users=User.query.order_by(User.created_at.desc()).all())
@app.route("/users/<uuid:user_id>/toggle", methods=["POST"])
@roles_required("admin")
def toggle_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id: flash("현재 로그인한 관리자 계정은 비활성화할 수 없습니다.", "error")
    else: user.is_active_flag = not user.is_active_flag; audit("user_status_changed", user=current_user, target_type="user", target_id=user.id, detail=f"active={user.is_active_flag}"); commit_or_rollback(); flash("사용자 상태가 변경되었습니다.", "success")
    return redirect(url_for("users"))
@app.route("/users/<uuid:user_id>/role", methods=["POST"])
@roles_required("admin")
def change_user_role(user_id):
    user = db.get_or_404(User, user_id); role = request.form.get("role")
    if role not in {"admin", "editor", "viewer"}: abort(400)
    if user.id == current_user.id and role != "admin": flash("현재 로그인한 관리자 본인의 관리자 권한은 해제할 수 없습니다.", "error")
    else: old_role = user.role; user.role = role; audit("user_role_changed", user=current_user, target_type="user", target_id=user.id, detail=f"{old_role}->{role}"); commit_or_rollback(); flash("사용자 권한이 변경되었습니다.", "success")
    return redirect(url_for("users"))
@app.route("/users/<uuid:user_id>/reset-password", methods=["POST"])
@roles_required("admin")
def reset_user_password(user_id):
    user = db.get_or_404(User, user_id); temp_password = uuid.uuid4().hex[:8] + "!9Aa"; user.password_hash = generate_password_hash(temp_password); user.must_change_password = True; audit("temporary_password_issued", user=current_user, target_type="user", target_id=user.id)
    if commit_or_rollback(): flash(f"임시 비밀번호가 발급되었습니다: {temp_password}", "success")
    else: flash("임시 비밀번호 발급에 실패했습니다.", "error")
    return redirect(url_for("users"))
@app.route("/profile")
@login_required
def profile(): return render_template("profile.html")
@app.route("/audit-logs")
@roles_required("admin")
def audit_logs(): return render_template("audit_logs.html", logs=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all())
@app.route("/api/session")
def api_session():
    if not current_user.is_authenticated: return jsonify({"error": "unauthorized"}), 401
    return jsonify({"id": str(current_user.id), "name": current_user.name, "role": current_user.role})
@app.route("/health")
@csrf.exempt
def health():
    result = {"status": "error", "database": False, "database_backend": "postgresql", "database_writable": False, "application_ready": False}
    if not db_config_present(): return jsonify(result), 503
    try:
        db.session.execute(text("SELECT 1")); result["database"] = True; db.session.execute(text("CREATE TEMP TABLE IF NOT EXISTS health_write_check (id integer) ON COMMIT DROP")); db.session.execute(text("INSERT INTO health_write_check(id) VALUES (1)")); db.session.rollback(); result.update(status="ok", database_writable=True, application_ready=True); return jsonify(result), 200
    except Exception: db.session.rollback(); return jsonify(result), 503
@app.errorhandler(403)
def forbidden(_): return render_template("403.html"), 403
@app.errorhandler(404)
def missing(_): return render_template("404.html"), 404
@app.errorhandler(413)
def too_large(_): flash("업로드 파일은 20MB 이하만 가능합니다.", "error"); return redirect(url_for("data_upload"))
@app.errorhandler(500)
def internal_error(_): db.session.rollback(); return render_template("500.html"), 500
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
