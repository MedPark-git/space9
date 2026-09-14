import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

KST = ZoneInfo("Asia/Seoul")
REQUIRED_DB_ENV = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
SCHEMA_VERSION = 1

def utcnow(): return datetime.now(timezone.utc)
def db_config_present(): return all(os.getenv(k) for k in REQUIRED_DB_ENV)
def database_uri():
    if not db_config_present():
        return "postgresql+psycopg://not-ready:not-ready@127.0.0.1:1/not-ready"
    return f"postgresql+psycopg://{quote_plus(os.environ['DB_USER'])}:{quote_plus(os.environ['DB_PASSWORD'])}@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"

app = Flask(__name__)
app.config.update(SECRET_KEY=os.getenv("SECRET_KEY") or os.urandom(32), SQLALCHEMY_DATABASE_URI=database_uri(), SQLALCHEMY_TRACK_MODIFICATIONS=False, SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True,"pool_recycle":300,"pool_size":5,"max_overflow":5}, PERMANENT_SESSION_LIFETIME=timedelta(minutes=60), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE","true").lower()=="true", SESSION_COOKIE_SAMESITE="Lax", WTF_CSRF_TIME_LIMIT=3600)
db=SQLAlchemy(app); csrf=CSRFProtect(app); login_manager=LoginManager(app); login_manager.login_view="login"; login_manager.login_message="로그인이 필요합니다."

class User(UserMixin, db.Model):
    __tablename__="users"
    id=db.Column(db.Uuid(as_uuid=True),primary_key=True,default=uuid.uuid4)
    login_id=db.Column(db.String(80),nullable=False,unique=True,index=True)
    name=db.Column(db.String(100),nullable=False); department=db.Column(db.String(100)); position=db.Column(db.String(100))
    role=db.Column(db.String(20),nullable=False,default="viewer"); password_hash=db.Column(db.String(255),nullable=False)
    is_active_flag=db.Column(db.Boolean,nullable=False,default=True); must_change_password=db.Column(db.Boolean,nullable=False,default=True)
    last_login_at=db.Column(db.DateTime(timezone=True)); created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utcnow); updated_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utcnow,onupdate=utcnow)
    @property
    def is_active(self): return self.is_active_flag
    def get_id(self): return str(self.id)

class AuditLog(db.Model):
    __tablename__="audit_logs"
    id=db.Column(db.Uuid(as_uuid=True),primary_key=True,default=uuid.uuid4); user_id=db.Column(db.Uuid(as_uuid=True),db.ForeignKey("users.id"),nullable=True,index=True)
    login_id=db.Column(db.String(80),index=True); action=db.Column(db.String(80),nullable=False,index=True); target_type=db.Column(db.String(80)); target_id=db.Column(db.String(120)); detail=db.Column(db.String(500)); ip_address=db.Column(db.String(64)); created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utcnow,index=True)

class CashJournal(db.Model):
    __tablename__="cash_journals"
    id=db.Column(db.Uuid(as_uuid=True),primary_key=True,default=uuid.uuid4); journal_date=db.Column(db.Date,nullable=False,index=True); status=db.Column(db.String(20),nullable=False,default="draft"); note=db.Column(db.Text); created_by=db.Column(db.Uuid(as_uuid=True),db.ForeignKey("users.id"),nullable=False); updated_by=db.Column(db.Uuid(as_uuid=True),db.ForeignKey("users.id"),nullable=False); created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utcnow); updated_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utcnow,onupdate=utcnow)

class SchemaVersion(db.Model):
    __tablename__="schema_versions"; version=db.Column(db.Integer,primary_key=True); applied_at=db.Column(db.DateTime(timezone=True),nullable=False,default=utcnow)

@login_manager.user_loader
def load_user(user_id):
    try: return db.session.get(User,uuid.UUID(user_id))
    except Exception: return None

def kst(value):
    if not value: return "-"
    if value.tzinfo is None: value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
app.jinja_env.filters["kst"]=kst

def audit(action,user=None,login_id=None,target_type=None,target_id=None,detail=None):
    db.session.add(AuditLog(user_id=getattr(user,"id",None),login_id=login_id or getattr(user,"login_id",None),action=action,target_type=target_type,target_id=str(target_id) if target_id else None,detail=(detail or "")[:500],ip_address=(request.headers.get("X-Forwarded-For",request.remote_addr or "").split(",")[0].strip())[:64]))

def commit_or_rollback():
    try: db.session.commit(); return True
    except SQLAlchemyError: db.session.rollback(); app.logger.exception("Database transaction failed"); return False

def ensure_schema_and_bootstrap():
    if not db_config_present(): raise RuntimeError("PostgreSQL 환경변수가 준비되지 않았습니다.")
    with db.engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(874221934)")); db.create_all(); current=conn.execute(text("SELECT COALESCE(MAX(version),0) FROM schema_versions")).scalar_one()
        if current<SCHEMA_VERSION: conn.execute(text("INSERT INTO schema_versions(version, applied_at) VALUES (:v, NOW()) ON CONFLICT DO NOTHING"),{"v":SCHEMA_VERSION})
    if User.query.count()==0:
        admin_id=os.getenv("BOOTSTRAP_ADMIN_ID"); admin_pw=os.getenv("BOOTSTRAP_ADMIN_PASSWORD"); admin_name=os.getenv("BOOTSTRAP_ADMIN_NAME","시스템관리자")
        if not admin_id or not admin_pw: raise RuntimeError("초기 관리자 환경변수가 준비되지 않았습니다.")
        db.session.add(User(login_id=admin_id.strip().lower(),name=admin_name,role="admin",password_hash=generate_password_hash(admin_pw),is_active_flag=True,must_change_password=True)); db.session.commit()

@app.before_request
def prepare_request():
    session.permanent=True
    if request.endpoint=="static": return None
    try: ensure_schema_and_bootstrap()
    except Exception:
        db.session.rollback()
        if request.path=="/health": return None
        if request.path.startswith("/api/"): return jsonify({"error":"application_not_ready"}),503
        if request.path!="/not-ready": return redirect(url_for("not_ready"))
    if current_user.is_authenticated and current_user.must_change_password and request.endpoint not in {"force_password_change","logout","not_ready"}: return redirect(url_for("force_password_change"))

@app.teardown_request
def teardown_request(exc):
    if exc: db.session.rollback()
    db.session.remove()

@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"): return jsonify({"error":"unauthorized"}),401
    return redirect(url_for("login",next=request.full_path))

def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args,**kwargs):
            if current_user.role not in roles: abort(403)
            return fn(*args,**kwargs)
        return wrapped
    return deco

def recent_failed_attempts(login_id): return AuditLog.query.filter(AuditLog.action=="login_failed",AuditLog.login_id==login_id,AuditLog.created_at>=utcnow()-timedelta(minutes=10)).count()

@app.route("/not-ready")
def not_ready(): return render_template("not_ready.html"),503

@app.route("/login",methods=["GET","POST"])
def login():
    if current_user.is_authenticated: return redirect(url_for("dashboard"))
    if request.method=="POST":
        login_id=(request.form.get("login_id") or "").strip().lower(); password=request.form.get("password") or ""
        if recent_failed_attempts(login_id)>=5: flash("로그인 실패 횟수가 초과되었습니다. 10분 후 다시 시도해 주세요.","error"); return render_template("login.html"),429
        user=User.query.filter(db.func.lower(User.login_id)==login_id).first()
        if not user or not user.is_active_flag or not check_password_hash(user.password_hash,password):
            audit("login_failed",login_id=login_id,detail="Invalid credentials or inactive account"); commit_or_rollback(); flash("아이디 또는 비밀번호를 확인해 주세요.","error"); return render_template("login.html"),401
        login_user(user); user.last_login_at=utcnow(); audit("login_success",user=user); commit_or_rollback(); return redirect(url_for("force_password_change" if user.must_change_password else "dashboard"))
    return render_template("login.html")

@app.route("/logout",methods=["POST"])
@login_required
def logout():
    audit("logout",user=current_user); commit_or_rollback(); logout_user(); session.clear(); response=redirect(url_for("login")); response.delete_cookie(app.config.get("SESSION_COOKIE_NAME","session")); return response

@app.route("/password/initial",methods=["GET","POST"])
@login_required
def force_password_change():
    if request.method=="POST":
        current=request.form.get("current_password") or ""; new1=request.form.get("new_password") or ""; new2=request.form.get("confirm_password") or ""
        if not check_password_hash(current_user.password_hash,current): flash("현재 비밀번호가 일치하지 않습니다.","error")
        elif len(new1)<10 or not any(c.isalpha() for c in new1) or not any(c.isdigit() for c in new1) or not any(not c.isalnum() for c in new1): flash("새 비밀번호는 10자 이상이며 영문·숫자·특수문자를 포함해야 합니다.","error")
        elif new1!=new2: flash("새 비밀번호 확인이 일치하지 않습니다.","error")
        elif check_password_hash(current_user.password_hash,new1): flash("기존 비밀번호와 다른 비밀번호를 사용해 주세요.","error")
        else:
            current_user.password_hash=generate_password_hash(new1); current_user.must_change_password=False; audit("password_changed",user=current_user)
            if commit_or_rollback(): flash("비밀번호가 변경되었습니다.","success"); return redirect(url_for("dashboard"))
            flash("비밀번호 변경에 실패했습니다.","error")
    return render_template("password_change.html",initial=True)

@app.route("/password/change",methods=["GET","POST"])
@login_required
def password_change(): return force_password_change()
@app.route("/")
@login_required
def dashboard(): return render_template("dashboard.html")
@app.route("/cash-journal")
@login_required
def cash_journal(): return render_template("cash_journal.html",rows=CashJournal.query.order_by(CashJournal.journal_date.desc()).limit(100).all())

@app.route("/users",methods=["GET","POST"])
@roles_required("admin")
def users():
    if request.method=="POST":
        login_id=(request.form.get("login_id") or "").strip().lower()
        if not login_id or User.query.filter(db.func.lower(User.login_id)==login_id).first(): flash("로그인 ID가 비어 있거나 이미 존재합니다.","error")
        else:
            temp_password=uuid.uuid4().hex[:8]+"!9Aa"; user=User(login_id=login_id,name=(request.form.get("name") or login_id).strip(),department=(request.form.get("department") or "").strip(),position=(request.form.get("position") or "").strip(),role=request.form.get("role") if request.form.get("role") in {"admin","editor","viewer"} else "viewer",password_hash=generate_password_hash(temp_password),must_change_password=True); db.session.add(user); audit("user_created",user=current_user,target_type="user",target_id=user.id,detail=f"login_id={login_id}")
            if commit_or_rollback(): flash(f"사용자가 생성되었습니다. 임시 비밀번호: {temp_password}","success")
            else: flash("사용자 생성에 실패했습니다.","error")
    return render_template("users.html",users=User.query.order_by(User.created_at.desc()).all())

@app.route("/users/<uuid:user_id>/toggle",methods=["POST"])
@roles_required("admin")
def toggle_user(user_id):
    user=db.get_or_404(User,user_id)
    if user.id==current_user.id: flash("현재 로그인한 관리자 계정은 비활성화할 수 없습니다.","error")
    else: user.is_active_flag=not user.is_active_flag; audit("user_status_changed",user=current_user,target_type="user",target_id=user.id,detail=f"active={user.is_active_flag}"); commit_or_rollback(); flash("사용자 상태가 변경되었습니다.","success")
    return redirect(url_for("users"))
@app.route("/profile")
@login_required
def profile(): return render_template("profile.html")
@app.route("/audit-logs")
@roles_required("admin")
def audit_logs(): return render_template("audit_logs.html",logs=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all())
@app.route("/api/session")
def api_session():
    if not current_user.is_authenticated: return jsonify({"error":"unauthorized"}),401
    return jsonify({"id":str(current_user.id),"name":current_user.name,"role":current_user.role})
@app.route("/health")
@csrf.exempt
def health():
    result={"status":"error","database":False,"database_backend":"postgresql","database_writable":False,"application_ready":False}
    if not db_config_present(): return jsonify(result),503
    try:
        db.session.execute(text("SELECT 1")); result["database"]=True; db.session.execute(text("CREATE TEMP TABLE IF NOT EXISTS health_write_check (id integer) ON COMMIT DROP")); db.session.execute(text("INSERT INTO health_write_check(id) VALUES (1)")); db.session.rollback(); result.update(status="ok",database_writable=True,application_ready=True); return jsonify(result),200
    except Exception: db.session.rollback(); return jsonify(result),503
@app.errorhandler(403)
def forbidden(_): return render_template("403.html"),403
@app.errorhandler(404)
def missing(_): return render_template("404.html"),404
@app.errorhandler(500)
def internal_error(_): db.session.rollback(); return render_template("500.html"),500
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","8000")),debug=False)
